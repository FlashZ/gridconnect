import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest

from app import database as db
from app import services


@pytest.fixture(autouse=True)
def temporary_database(tmp_path, monkeypatch):
    path = tmp_path / "gridconnect.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    db.initialise()
    return path


def add_device(name="Plug", device_id="tuya-1"):
    with db.connection() as con:
        cursor = con.execute(
            """INSERT INTO devices(name,ip_address,tuya_device_id,local_key,protocol_version)
               VALUES(?,?,?,?,?)""",
            (name, "127.0.0.1", device_id, "local-key", 3.3),
        )
    return cursor.lastrowid


def test_meter_reset_only_discards_the_reset_interval():
    device_id = add_device()
    services.persist_reading(
        device_id,
        {"is_on": True, "watts": 10, "voltage": 230, "amps": 0.1, "energy_total_kwh": 10},
    )
    services.persist_reading(
        device_id,
        {"is_on": True, "watts": 10, "voltage": 230, "amps": 0.1, "energy_total_kwh": 0},
    )
    services.persist_reading(
        device_id,
        {"is_on": True, "watts": 10, "voltage": 230, "amps": 0.1, "energy_total_kwh": 0.2},
    )
    deltas = [row["energy_delta_kwh"] for row in db.rows("SELECT energy_delta_kwh FROM readings")]
    assert deltas == [0, 0, pytest.approx(0.2)]


def test_failed_schedule_is_claimed_exactly_once(monkeypatch):
    device_id = add_device()
    now = datetime.now(services.ZoneInfo(db.settings()["timezone"]))
    with db.connection() as con:
        con.execute(
            "INSERT INTO schedules(device_id,days,at_time,action,enabled) VALUES(?,?,?,?,1)",
            (device_id, str(now.weekday()), now.strftime("%H:%M"), "on"),
        )

    def fail(*_args):
        raise RuntimeError("offline")

    monkeypatch.setattr(services, "control_device", fail)
    asyncio.run(services.run_due_schedules())
    asyncio.run(services.run_due_schedules())

    assert db.row("SELECT COUNT(*) AS count FROM automation_executions")["count"] == 1
    assert db.row("SELECT last_run_date FROM schedules")["last_run_date"] == now.date().isoformat()


def test_failed_timer_retries_three_times_then_stops(monkeypatch):
    device_id = add_device()
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    with db.connection() as con:
        con.execute(
            "INSERT INTO timers(device_id,action,run_at,created_at) VALUES(?,?,?,?)",
            (device_id, "off", past, past),
        )

    def fail(*_args):
        raise RuntimeError("offline")

    monkeypatch.setattr(services, "control_device", fail)
    for _ in range(3):
        asyncio.run(services.run_due_timers())
        with db.connection() as con:
            con.execute("UPDATE timers SET next_attempt_at=?", (past,))

    timer = db.row("SELECT attempt_count,failed_at FROM timers")
    assert timer["attempt_count"] == 3
    assert timer["failed_at"] is not None
    asyncio.run(services.run_due_timers())
    assert db.row("SELECT COUNT(*) AS count FROM automation_executions")["count"] == 3


def test_rollup_watt_average_is_sample_weighted(monkeypatch):
    monkeypatch.setattr(
        services,
        "_history_rows",
        lambda *_args, **_kwargs: [
            {
                "captured_at": "2026-01-01T10:00:00Z",
                "energy_delta_kwh": 1,
                "watts": 100,
                "samples": 1,
            },
            {
                "captured_at": "2026-01-01T11:00:00Z",
                "energy_delta_kwh": 1,
                "watts": 10,
                "samples": 9,
            },
        ],
    )
    result = services.trend_rows(24, "day")
    assert result[0]["avg_watts"] == 19.0
    assert result[0]["samples"] == 10


def test_devices_poll_concurrently(monkeypatch):
    for index in range(4):
        add_device(f"Plug {index}", f"tuya-{index}")

    def slow_success(_device):
        time.sleep(0.15)
        return {"is_on": True, "watts": 1, "voltage": 230, "amps": 0.1, "energy_total_kwh": 1}

    monkeypatch.setattr(services, "poll_with_retry", slow_success)
    started = time.perf_counter()
    asyncio.run(services.poll_all(concurrency=4))
    elapsed = time.perf_counter() - started

    assert elapsed < 0.45
    assert db.row("SELECT COUNT(*) AS count FROM readings")["count"] == 4


def test_rollup_accumulates_instead_of_overwriting_a_bucket():
    device_id = add_device()
    old = (datetime.now(UTC) - timedelta(days=200)).replace(minute=5)
    with db.connection() as con:
        con.execute(
            """INSERT INTO readings(device_id,captured_at,watts,energy_total_kwh,energy_delta_kwh)
               VALUES(?,?,?,?,?)""",
            (device_id, old.isoformat(), 100.0, 1.0, 1.0),
        )
    assert services.archive_readings() == 1
    first = db.row("SELECT kwh,samples FROM energy_rollups")
    assert first["kwh"] == pytest.approx(1.0)

    # A late sample lands in the same hourly bucket on a later archive run.
    with db.connection() as con:
        con.execute(
            """INSERT INTO readings(device_id,captured_at,watts,energy_total_kwh,energy_delta_kwh)
               VALUES(?,?,?,?,?)""",
            (device_id, old.replace(minute=45).isoformat(), 50.0, 3.0, 2.0),
        )
    services.archive_readings()
    merged = db.row("SELECT kwh,samples,avg_watts FROM energy_rollups")
    assert merged["kwh"] == pytest.approx(3.0), "earlier archived energy must not be discarded"
    assert merged["samples"] == 2
    assert merged["avg_watts"] == pytest.approx(75.0)


def test_sustained_load_needs_a_full_window_above_the_threshold():
    device_id = add_device()
    now = datetime.now(UTC)
    for minutes in range(20, -1, -1):
        with db.connection() as con:
            con.execute(
                "INSERT INTO readings(device_id,captured_at,watts,energy_delta_kwh) VALUES(?,?,?,0)",
                (device_id, (now - timedelta(minutes=minutes)).isoformat(), 2000.0),
            )
    assert services.sustained_load(device_id, 1800, 15) == pytest.approx(2000.0)
    # One dip below the line means the draw was not continuous.
    with db.connection() as con:
        con.execute(
            "INSERT INTO readings(device_id,captured_at,watts,energy_delta_kwh) VALUES(?,?,?,0)",
            (device_id, (now - timedelta(minutes=5, seconds=30)).isoformat(), 10.0),
        )
    assert services.sustained_load(device_id, 1800, 15) is None


def test_sustained_load_ignores_a_window_that_only_just_started():
    device_id = add_device()
    now = datetime.now(UTC)
    for minutes in (2, 1, 0):
        with db.connection() as con:
            con.execute(
                "INSERT INTO readings(device_id,captured_at,watts,energy_delta_kwh) VALUES(?,?,?,0)",
                (device_id, (now - timedelta(minutes=minutes)).isoformat(), 2400.0),
            )
    assert services.sustained_load(device_id, 1800, 15) is None


def test_check_power_alerts_flags_sustained_draw_and_odd_voltage():
    device_id = add_device()
    now = datetime.now(UTC)
    for minutes in range(20, -1, -1):
        with db.connection() as con:
            con.execute(
                "INSERT INTO readings(device_id,captured_at,watts,energy_delta_kwh) VALUES(?,?,?,0)",
                (device_id, (now - timedelta(minutes=minutes)).isoformat(), 2000.0),
            )
    device = db.row("SELECT * FROM devices WHERE id=?", (device_id,))
    device["max_watts"] = 2400
    services.check_power_alerts(
        device, {"watts": 2000.0, "voltage": 260.9, "amps": 8.6, "energy_total_kwh": 1.0}
    )
    kinds = {row["kind"] for row in db.rows("SELECT kind FROM alerts WHERE resolved_at IS NULL")}
    assert "sustained_load" in kinds
    assert "voltage" in kinds
    assert "high_watts" not in kinds, "2000 W is under the 2400 W peak limit"


def test_budget_alert_opens_and_clears():
    device_id = add_device()
    with db.connection() as con:
        con.execute("UPDATE devices SET monthly_budget_cents=100 WHERE id=?", (device_id,))
    services.persist_reading(
        device_id,
        {"is_on": True, "watts": 10, "voltage": 230, "amps": 0.1, "energy_total_kwh": 0},
    )
    services.persist_reading(
        device_id,
        {"is_on": True, "watts": 10, "voltage": 230, "amps": 0.1, "energy_total_kwh": 50},
    )
    services.check_budget_alerts()
    assert db.row("SELECT COUNT(*) AS count FROM alerts WHERE kind='budget'")["count"] == 1

    with db.connection() as con:
        con.execute("UPDATE devices SET monthly_budget_cents=1000000 WHERE id=?", (device_id,))
    services.check_budget_alerts()
    assert db.row("SELECT resolved_at FROM alerts WHERE kind='budget'")["resolved_at"] is not None


def test_live_power_total_ignores_offline_and_stale_plugs():
    live = add_device("Live", "tuya-live")
    dropped = add_device("Dropped", "tuya-dropped")
    services.persist_reading(
        live, {"is_on": True, "watts": 120, "voltage": 230, "amps": 0.5, "energy_total_kwh": 1}
    )
    services.persist_reading(
        dropped, {"is_on": True, "watts": 900, "voltage": 230, "amps": 4, "energy_total_kwh": 1}
    )
    stale = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    with db.connection() as con:
        con.execute("UPDATE devices SET online=0 WHERE id=?", (dropped,))
        con.execute("UPDATE readings SET captured_at=? WHERE device_id=?", (stale, dropped))
    assert services.live_power_total() == pytest.approx(120.0)


def test_schedule_still_runs_after_a_missed_minute(monkeypatch):
    device_id = add_device()
    tz = services.ZoneInfo(db.settings()["timezone"])
    now = datetime.now(tz)
    due = now - timedelta(minutes=4)
    with db.connection() as con:
        con.execute(
            "INSERT INTO schedules(device_id,days,at_time,action,enabled) VALUES(?,?,?,?,1)",
            (device_id, str(due.weekday()), due.strftime("%H:%M"), "on"),
        )
    calls = []
    monkeypatch.setattr(services, "control_device", lambda *a: calls.append(a))
    asyncio.run(services.run_due_schedules())
    assert len(calls) == 1, "a schedule missed by a slow poll cycle must still fire"


def test_schedule_does_not_replay_hours_later(monkeypatch):
    device_id = add_device()
    tz = services.ZoneInfo(db.settings()["timezone"])
    due = datetime.now(tz) - timedelta(hours=6)
    with db.connection() as con:
        con.execute(
            "INSERT INTO schedules(device_id,days,at_time,action,enabled) VALUES(?,?,?,?,1)",
            (device_id, str(due.weekday()), due.strftime("%H:%M"), "on"),
        )
    calls = []
    monkeypatch.setattr(services, "control_device", lambda *a: calls.append(a))
    asyncio.run(services.run_due_schedules())
    assert calls == []


def test_polling_loop_step_survives_a_failing_stage():
    async def boom():
        raise RuntimeError("database is locked")

    asyncio.run(services._run_step("poll", boom()))
    alert = db.row("SELECT kind,device_id FROM alerts WHERE resolved_at IS NULL")
    assert alert["kind"] == "internal_poll"
    assert alert["device_id"] is None

    async def fine():
        return None

    asyncio.run(services._run_step("poll", fine()))
    assert db.row("SELECT resolved_at FROM alerts")["resolved_at"] is not None
