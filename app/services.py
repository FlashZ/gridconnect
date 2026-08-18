import asyncio
import logging
import socket
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from . import database as db

logger = logging.getLogger("gridconnect")


def _float(value, scale):
    try:
        return float(value) * float(scale)
    except (TypeError, ValueError):
        return None


def _dps_value(dps, key):
    return dps.get(str(key), dps.get(key))


def _bool(value):
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "on", "yes"}
    return bool(value)


def poll_device(device: dict) -> dict:
    """Poll one LAN device. All DPS fields/scales can be tailored per plug in Settings."""
    import tinytuya

    outlet = tinytuya.OutletDevice(
        device["tuya_device_id"],
        device["ip_address"],
        device["local_key"],
        port=int(device.get("tuya_port", 6668)),
    )
    outlet.set_version(float(device["protocol_version"]))
    status = outlet.status()
    if not isinstance(status, dict) or status.get("Error"):
        raise RuntimeError((status or {}).get("Error", "No response"))
    dps = status.get("dps", {})
    return {
        "is_on": _bool(_dps_value(dps, device["switch_dps"])),
        "watts": _float(_dps_value(dps, device["watts_dps"]), device["power_scale"]),
        "voltage": _float(_dps_value(dps, device["voltage_dps"]), device["voltage_scale"]),
        "amps": _float(_dps_value(dps, device["current_dps"]), device["current_scale"]),
        "energy_total_kwh": _float(_dps_value(dps, device["energy_dps"]), device["energy_scale"]),
    }


def poll_with_retry(device: dict, attempts: int = 2) -> dict:
    """Retry one dropped Tuya LAN status packet before reporting failure."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return poll_device(device)
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1.5)
    raise RuntimeError(str(last_error) or "No response")


def control_device(device: dict, on: bool) -> None:
    import tinytuya

    outlet = tinytuya.OutletDevice(
        device["tuya_device_id"],
        device["ip_address"],
        device["local_key"],
        port=int(device.get("tuya_port", 6668)),
    )
    outlet.set_version(float(device["protocol_version"]))
    result = outlet.set_value(int(device["switch_dps"]), on)
    if isinstance(result, dict) and result.get("Error"):
        raise RuntimeError(result["Error"])


def test_lan_endpoint(ip_address: str, tuya_port: int = 6668) -> bool:
    """Confirm that a device exposes Tuya's local TCP endpoint without needing its key."""
    with socket.create_connection((ip_address, tuya_port), timeout=3):
        return True


def cloud_devices(
    api_region: str, api_key: str, api_secret: str, api_device_id: str | None = None
) -> list[dict]:
    """Fetch devices and local keys from a user-linked Tuya Cloud project.

    Credentials are intentionally never written to SQLite. The returned local key is
    sent straight to the browser's one-time import flow over the local connection.
    """
    import tinytuya

    cloud = tinytuya.Cloud(
        apiRegion=api_region,
        apiKey=api_key,
        apiSecret=api_secret,
        apiDeviceID=api_device_id or None,
    )
    devices = cloud.getdevices()
    if isinstance(devices, dict):
        raise RuntimeError(
            devices.get("Err") or devices.get("Error") or devices.get("error") or str(devices)
        )
    return [
        {
            "name": item.get("name", "Unnamed Tuya device"),
            "tuya_device_id": item.get("id", ""),
            "local_key": item.get("key", ""),
            "mac": item.get("mac", ""),
            "category": item.get("category", ""),
            "product_id": item.get("product_id", ""),
        }
        for item in devices
        if item.get("id") and item.get("key")
    ]


def detect_protocol(
    ip_address: str, tuya_device_id: str, local_key: str, tuya_port: int = 6668
) -> dict:
    """Identify the first Tuya LAN protocol version that returns a valid DPS map.

    The ascending order avoids accepting a newer compatibility mode when a device
    is actually an older protocol (for example a v3.2 plug that also replies to a
    v3.3 request).
    """
    import tinytuya

    errors = []
    for version in (3.1, 3.2, 3.3, 3.4, 3.5):
        try:
            outlet = tinytuya.OutletDevice(
                tuya_device_id, ip_address, local_key, version=version, port=tuya_port
            )
            status = outlet.status()
            if isinstance(status, dict) and isinstance(status.get("dps"), dict):
                return {"protocol_version": version, "dps": status["dps"]}
            errors.append(f"{version}: {(status or {}).get('Error', 'invalid response')}")
        except Exception as exc:
            errors.append(f"{version}: {exc}")
    raise RuntimeError("No supported Tuya protocol returned a valid status. " + "; ".join(errors))


def read_raw_dps(device: dict) -> dict:
    """Return a plug's raw DPS map alongside a guess at what each channel means.

    Metering plugs vary in which DPS carries power, voltage and current, and a
    wrong mapping is indistinguishable from a plug that reports nothing. Showing
    the raw values lets the mapping be corrected without trial and error.
    """
    import tinytuya

    outlet = tinytuya.OutletDevice(
        device["tuya_device_id"],
        device["ip_address"],
        device["local_key"],
        port=int(device.get("tuya_port", 6668)),
    )
    outlet.set_version(float(device["protocol_version"]))
    status = outlet.status()
    if not isinstance(status, dict) or status.get("Error"):
        raise RuntimeError((status or {}).get("Error", "No response"))
    dps = status.get("dps", {}) or {}

    mapped = {
        str(device.get("switch_dps", "1")): "switch",
        str(device.get("watts_dps", "19")): "watts",
        str(device.get("voltage_dps", "20")): "voltage",
        str(device.get("current_dps", "18")): "current",
        str(device.get("energy_dps", "17")): "energy",
    }
    channels = []
    for key, value in sorted(dps.items(), key=lambda item: (len(item[0]), item[0])):
        guess = None
        if isinstance(value, bool):
            guess = "switch"
        elif isinstance(value, int | float):
            # Typical Tuya metering plugs report tenths of a volt, milliamps and
            # tenths of a watt, which separates the three channels by magnitude.
            if 1800 <= value <= 2800:
                guess = "voltage (x0.1)"
            elif 0 < value <= 60:
                guess = "current (x0.001) or small watts"
            elif 60 < value <= 30000:
                guess = "watts (x0.1) or energy (x0.01)"
        channels.append(
            {
                "dps": key,
                "value": value,
                "assigned_to": mapped.get(key),
                "looks_like": guess,
            }
        )
    return {
        "dps": dps,
        "channels": channels,
        "decoded_with_current_mapping": {
            "is_on": _bool(_dps_value(dps, device["switch_dps"])),
            "watts": _float(_dps_value(dps, device["watts_dps"]), device["power_scale"]),
            "voltage": _float(_dps_value(dps, device["voltage_dps"]), device["voltage_scale"]),
            "amps": _float(_dps_value(dps, device["current_dps"]), device["current_scale"]),
            "energy_total_kwh": _float(
                _dps_value(dps, device["energy_dps"]), device["energy_scale"]
            ),
        },
    }


def persist_reading(device_id: int, reading: dict) -> None:
    now = db.utc_now()
    previous = db.row(
        "SELECT energy_total_kwh FROM readings WHERE device_id=? AND energy_total_kwh IS NOT NULL ORDER BY captured_at DESC LIMIT 1",
        (device_id,),
    )
    total = reading["energy_total_kwh"]
    delta = max(0, total - previous["energy_total_kwh"]) if total is not None and previous else 0
    with db.connection() as con:
        con.execute(
            "INSERT INTO readings(device_id,captured_at,is_on,watts,voltage,amps,energy_total_kwh,energy_delta_kwh) VALUES(?,?,?,?,?,?,?,?)",
            (
                device_id,
                now,
                int(reading["is_on"]),
                reading["watts"],
                reading["voltage"],
                reading["amps"],
                total,
                delta,
            ),
        )
        con.execute(
            "UPDATE devices SET online=1,last_seen=?,last_error=NULL WHERE id=?",
            (now, device_id),
        )


def open_alert(device_id: int | None, kind: str, message: str) -> None:
    """Open one deduplicated operational alert until a successful check resolves it."""
    if not db.settings().get("alerts_enabled", True):
        return
    with db.connection() as con:
        existing = con.execute(
            "SELECT id FROM alerts WHERE device_id IS ? AND kind=? AND resolved_at IS NULL",
            (device_id, kind),
        ).fetchone()
        if existing is None:
            con.execute(
                "INSERT INTO alerts(device_id,kind,message,created_at) VALUES(?,?,?,?)",
                (device_id, kind, message[:500], db.utc_now()),
            )


def resolve_alerts(device_id: int, kinds: tuple[str, ...]) -> None:
    if not kinds:
        return
    with db.connection() as con:
        con.execute(
            f"UPDATE alerts SET resolved_at=? WHERE device_id=? AND kind IN ({','.join('?' for _ in kinds)}) AND resolved_at IS NULL",
            (db.utc_now(), device_id, *kinds),
        )


def resolve_system_alerts(kinds: tuple[str, ...]) -> None:
    """Clear alerts that belong to the service itself rather than to a plug."""
    if not kinds:
        return
    with db.connection() as con:
        con.execute(
            f"UPDATE alerts SET resolved_at=? WHERE device_id IS NULL AND kind IN ({','.join('?' for _ in kinds)}) AND resolved_at IS NULL",
            (db.utc_now(), *kinds),
        )


def reading_is_stale(captured_at: str | None, config: dict | None = None) -> bool:
    """A reading older than a few poll cycles no longer describes the plug's draw."""
    if not captured_at:
        return True
    config = config or db.settings()
    interval = max(3, int(config["poll_interval_seconds"]))
    age = datetime.now(UTC) - datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    return age > timedelta(seconds=max(60, interval * 4))


def live_power_total() -> float:
    """Total current draw, counting only plugs that are actually reporting.

    A plug that dropped off Wi-Fi keeps its last reading in the database. Including
    that in the headline figure made an offline plug look like a live load.
    """
    config = db.settings()
    rows = db.rows(
        """SELECT d.id, r.watts, r.captured_at
           FROM devices d LEFT JOIN readings r
             ON r.id=(SELECT id FROM readings WHERE device_id=d.id ORDER BY captured_at DESC LIMIT 1)
           WHERE d.archived_at IS NULL AND d.enabled=1 AND d.online=1"""
    )
    return round(
        sum(
            (row["watts"] or 0) for row in rows if not reading_is_stale(row["captured_at"], config)
        ),
        1,
    )


def archive_readings() -> int:
    """Roll old raw samples into hourly summaries before pruning them."""
    days = max(1, int(db.settings().get("raw_retention_days", 90)))
    cutoff = datetime.now(UTC) - timedelta(days=days)
    with db.connection() as con:
        con.execute(
            """INSERT INTO energy_rollups(device_id,bucket_start,bucket_type,kwh,avg_watts,samples)
                       SELECT device_id, strftime('%Y-%m-%dT%H:00:00Z', captured_at), 'hour',
                              COALESCE(SUM(energy_delta_kwh),0), AVG(watts), COUNT(*)
                       FROM readings WHERE captured_at<? GROUP BY device_id, strftime('%Y-%m-%dT%H:00:00Z', captured_at)
                       ON CONFLICT(device_id,bucket_start,bucket_type) DO UPDATE SET
                         kwh=energy_rollups.kwh+excluded.kwh,
                         avg_watts=CASE
                           WHEN energy_rollups.samples+excluded.samples=0 THEN excluded.avg_watts
                           ELSE (COALESCE(energy_rollups.avg_watts,0)*energy_rollups.samples
                                 +COALESCE(excluded.avg_watts,0)*excluded.samples)
                                /(energy_rollups.samples+excluded.samples) END,
                         samples=energy_rollups.samples+excluded.samples""",
            (cutoff.isoformat(),),
        )
        deleted = con.execute(
            "DELETE FROM readings WHERE captured_at<?", (cutoff.isoformat(),)
        ).rowcount
    return deleted


def _history_rows(
    start_utc: datetime, end_utc: datetime, device_id: int | None = None
) -> list[dict]:
    clause, params = "", []
    if device_id is not None:
        clause, params = " AND device_id=?", [device_id]
    return db.rows(
        f"""SELECT device_id,captured_at,energy_delta_kwh,watts,1 AS samples FROM readings
                       WHERE captured_at>=? AND captured_at<?{clause}
                       UNION ALL
                       SELECT device_id,bucket_start AS captured_at,kwh AS energy_delta_kwh,avg_watts AS watts,samples FROM energy_rollups
                       WHERE bucket_start>=? AND bucket_start<?{clause}
                       ORDER BY captured_at""",
        [
            start_utc.isoformat(),
            end_utc.isoformat(),
            *params,
            start_utc.isoformat(),
            end_utc.isoformat(),
            *params,
        ],
    )


def mark_poll_failure(device: dict, exc: Exception, prefix: str = "") -> None:
    """Record a failed poll while retaining recent healthy state during Wi-Fi blips."""
    config = db.settings()
    interval = max(3, int(config["poll_interval_seconds"]))
    grace_cutoff = (datetime.now(UTC) - timedelta(seconds=max(60, interval * 4))).isoformat()
    error = f"{prefix}{str(exc) or 'No response'}"[:500]
    with db.connection() as con:
        con.execute(
            """UPDATE devices
               SET online=CASE WHEN last_seen IS NOT NULL AND last_seen>=? THEN 1 ELSE 0 END,
                   last_error=? WHERE id=?""",
            (grace_cutoff, error, device["id"]),
        )
    last_seen = device.get("last_seen")
    threshold = max(0, int(config.get("alert_offline_minutes", 5)))
    if not last_seen or datetime.now(UTC) - datetime.fromisoformat(
        last_seen.replace("Z", "+00:00")
    ) >= timedelta(minutes=threshold):
        open_alert(device["id"], "offline", f"{device['name']} is unreachable: {error[:240]}")


def sustained_load(device_id: int, threshold_watts: float, minutes: int) -> float | None:
    """Return the mean draw if a device has stayed above a threshold for the whole window.

    A plug run near its rating for hours is a very different hazard from a brief
    inrush peak, so this deliberately ignores windows that dipped below the line.
    """
    if threshold_watts <= 0 or minutes <= 0:
        return None
    since = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()
    window = db.row(
        """SELECT COUNT(*) AS samples, MIN(watts) AS low, AVG(watts) AS mean,
                  MIN(captured_at) AS started
           FROM readings WHERE device_id=? AND captured_at>=? AND watts IS NOT NULL""",
        (device_id, since),
    )
    if not window or (window["samples"] or 0) < 3 or window["low"] is None:
        return None
    # Require the window to actually span the period, so a plug that has only just
    # come back online cannot trip the alert on two high samples.
    started = datetime.fromisoformat(window["started"].replace("Z", "+00:00"))
    if datetime.now(UTC) - started < timedelta(minutes=minutes) * 0.8:
        return None
    return float(window["mean"]) if window["low"] >= threshold_watts else None


def check_power_alerts(device: dict, reading: dict, config: dict | None = None) -> None:
    """Raise peak, sustained-load and supply-voltage alerts for one fresh reading."""
    config = config or db.settings()
    watts, volts = reading.get("watts"), reading.get("voltage")
    limit = float(device["max_watts"]) if device.get("max_watts") else None

    if limit and watts is not None and watts > limit:
        open_alert(
            device["id"],
            "high_watts",
            f"{device['name']} is drawing {watts:.1f} W, above its {limit:.1f} W limit.",
        )
    else:
        resolve_alerts(device["id"], ("high_watts",))

    minutes = max(0, int(config.get("sustained_load_minutes", 15)))
    percent = max(0.0, float(config.get("sustained_load_percent", 80.0)))
    if limit and minutes and percent:
        threshold = limit * percent / 100
        mean = sustained_load(device["id"], threshold, minutes)
        if mean is not None:
            open_alert(
                device["id"],
                "sustained_load",
                f"{device['name']} has drawn at least {threshold:.0f} W "
                f"({percent:.0f}% of its {limit:.0f} W limit) continuously for {minutes} minutes, "
                f"averaging {mean:.0f} W. Check the plug and its lead for heat.",
            )
        else:
            resolve_alerts(device["id"], ("sustained_load",))

    nominal = float(config.get("nominal_voltage", 230.0) or 0)
    tolerance = float(config.get("voltage_tolerance_percent", 10.0) or 0)
    if nominal and tolerance and volts is not None and volts > 0:
        low, high = nominal * (1 - tolerance / 100), nominal * (1 + tolerance / 100)
        if not low <= volts <= high:
            open_alert(
                device["id"],
                "voltage",
                f"{device['name']} reported {volts:.1f} V, outside the expected "
                f"{low:.0f}-{high:.0f} V band. If the plug is working normally, its "
                f"voltage scale is probably wrong rather than the supply.",
            )
        else:
            resolve_alerts(device["id"], ("voltage",))


def check_budget_alerts(config: dict | None = None) -> None:
    """Compare this month's spend against each plug's budget."""
    config = config or db.settings()
    devices = db.rows(
        "SELECT id,name,monthly_budget_cents FROM devices "
        "WHERE monthly_budget_cents IS NOT NULL AND archived_at IS NULL AND enabled=1"
    )
    if not devices:
        return
    now = datetime.now(ZoneInfo(config["timezone"]))
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    totals = device_summaries(month_start, now)
    for device in devices:
        budget = float(device["monthly_budget_cents"]) / 100
        spent = totals.get(device["id"], {}).get("energy_cost", 0.0)
        if budget > 0 and spent >= budget:
            open_alert(
                device["id"],
                "budget",
                f"{device['name']} has used {spent:.2f} of its {budget:.2f} "
                f"{config['currency']} monthly budget.",
            )
        else:
            resolve_alerts(device["id"], ("budget",))


async def _poll_one(device: dict, semaphore: asyncio.Semaphore, config: dict | None = None) -> None:
    async with semaphore:
        try:
            # Tuya-over-relay replies can take several seconds on Wi-Fi. A bounded
            # wait avoids declaring a healthy plug offline on a slow status reply.
            reading = await asyncio.wait_for(asyncio.to_thread(poll_with_retry, device), timeout=30)
            persist_reading(device["id"], reading)
            resolve_alerts(device["id"], ("offline",))
            check_power_alerts(device, reading, config)
        except Exception as exc:
            mark_poll_failure(device, exc)


async def poll_all(concurrency: int = 4) -> None:
    """Poll plugs independently so one unreachable device cannot stall the rest."""
    devices = db.rows("SELECT * FROM devices WHERE enabled=1 AND archived_at IS NULL")
    if not devices:
        return
    config = db.settings()
    semaphore = asyncio.Semaphore(max(1, concurrency))
    await asyncio.gather(*(_poll_one(device, semaphore, config) for device in devices))


def log_automation(
    kind: str,
    source_id: int,
    device: dict,
    action: str,
    scheduled_for: str,
    status: str,
    attempt: int = 1,
    message: str | None = None,
) -> None:
    with db.connection() as con:
        con.execute(
            """INSERT INTO automation_executions
               (kind,source_id,device_id,device_name,action,scheduled_for,status,attempt,message,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                kind,
                source_id,
                device["device_id"],
                device["name"],
                action,
                scheduled_for,
                status,
                attempt,
                (message or "")[:500] or None,
                db.utc_now(),
            ),
        )


SCHEDULE_CATCHUP_MINUTES = 10


async def run_due_schedules() -> None:
    tz = ZoneInfo(db.settings()["timezone"])
    now = datetime.now(tz)
    today = now.date().isoformat()
    for schedule in db.rows(
        "SELECT s.*, d.* FROM schedules s JOIN devices d ON d.id=s.device_id WHERE s.enabled=1 AND d.enabled=1 AND d.archived_at IS NULL"
    ):
        if (
            now.weekday() not in [int(day) for day in schedule["days"].split(",") if day]
            or schedule["last_run_date"] == today
        ):
            continue
        # Match a window rather than the exact minute: a slow poll cycle or a brief
        # restart must not silently skip the occurrence altogether. The window is
        # bounded so a service started hours later does not replay the morning.
        due_hour, due_minute = (int(part) for part in schedule["at_time"].split(":"))
        due_at = now.replace(hour=due_hour, minute=due_minute, second=0, microsecond=0)
        if not timedelta(0) <= now - due_at <= timedelta(minutes=SCHEDULE_CATCHUP_MINUTES):
            continue
        minute = schedule["at_time"]
        # Claim this occurrence before touching the device. This guarantees that a
        # slow or failed request is never repeated every poll for the same minute.
        with db.connection() as con:
            con.execute(
                "UPDATE schedules SET last_run_date=? WHERE id=?",
                (today, schedule["id"]),
            )
        scheduled_for = f"{today}T{minute}:00{now.strftime('%z')}"
        try:
            await asyncio.to_thread(control_device, schedule, schedule["action"] == "on")
            log_automation(
                "schedule",
                schedule["id"],
                schedule,
                schedule["action"],
                scheduled_for,
                "success",
            )
        except Exception as exc:
            with db.connection() as con:
                con.execute(
                    "UPDATE devices SET last_error=? WHERE id=?",
                    (f"Schedule failed: {exc}"[:500], schedule["device_id"]),
                )
            open_alert(
                schedule["device_id"],
                "schedule",
                f"Schedule for {schedule['name']} failed: {exc}",
            )
            log_automation(
                "schedule",
                schedule["id"],
                schedule,
                schedule["action"],
                scheduled_for,
                "failed",
                message=str(exc),
            )


async def run_due_timers() -> None:
    now = datetime.now(UTC).isoformat()
    timers = db.rows(
        """SELECT t.*,d.* FROM timers t JOIN devices d ON d.id=t.device_id
           WHERE t.failed_at IS NULL AND d.archived_at IS NULL
             AND COALESCE(t.next_attempt_at,t.run_at)<=? ORDER BY t.run_at""",
        (now,),
    )
    for timer in timers:
        attempt = int(timer.get("attempt_count") or 0) + 1
        try:
            await asyncio.to_thread(control_device, timer, timer["action"] == "on")
            log_automation(
                "timer",
                timer["id"],
                timer,
                timer["action"],
                timer["run_at"],
                "success",
                attempt,
            )
            with db.connection() as con:
                con.execute("DELETE FROM timers WHERE id=?", (timer["id"],))
        except Exception as exc:
            failed = attempt >= 3
            next_attempt = None
            if not failed:
                next_attempt = (
                    datetime.now(UTC) + timedelta(seconds=30 * (2 ** (attempt - 1)))
                ).isoformat()
            with db.connection() as con:
                con.execute(
                    """UPDATE timers SET last_error=?,attempt_count=?,next_attempt_at=?,failed_at=?
                       WHERE id=?""",
                    (
                        str(exc)[:500],
                        attempt,
                        next_attempt,
                        db.utc_now() if failed else None,
                        timer["id"],
                    ),
                )
            open_alert(timer["device_id"], "timer", f"Timer for {timer['name']} failed: {exc}")
            log_automation(
                "timer",
                timer["id"],
                timer,
                timer["action"],
                timer["run_at"],
                "failed" if failed else "retrying",
                attempt,
                str(exc),
            )


def rate_at(timestamp: str, settings: dict) -> float:
    if settings["tariff_mode"] == "flat":
        return float(settings["flat_rate_cents"])
    local = datetime.fromisoformat(timestamp).astimezone(ZoneInfo(settings["timezone"]))
    clock = local.strftime("%H:%M")
    for period in settings.get("tou_periods", []):
        start, end = period["start"], period["end"]
        in_window = start <= clock < end if start < end else clock >= start or clock < end
        if local.weekday() in period.get("days", []) and in_window:
            return float(period["rate_cents"])
    return float(settings["flat_rate_cents"])


def summary(start: datetime, end: datetime) -> dict:
    config = db.settings()
    start_utc, end_utc = start.astimezone(UTC), end.astimezone(UTC)
    data = _history_rows(start_utc, end_utc)
    energy = sum(x["energy_delta_kwh"] for x in data)
    gst_multiplier = 1 if config.get("gst_included", True) else 1.15
    energy_cost = sum(
        x["energy_delta_kwh"] * rate_at(x["captured_at"], config) / 100 * gst_multiplier
        for x in data
    )
    return {"kwh": round(energy, 3), "energy_cost": round(energy_cost, 2)}


def device_summaries(start: datetime, end: datetime) -> dict[int, dict]:
    """Return energy-only totals for each device in a period.

    Daily supply is intentionally excluded: it belongs to the household tariff,
    rather than to any particular plug.
    """
    config = db.settings()
    start_utc, end_utc = start.astimezone(UTC), end.astimezone(UTC)
    data = _history_rows(start_utc, end_utc)
    gst_multiplier = 1 if config.get("gst_included", True) else 1.15
    totals: dict[int, dict] = {}
    for item in data:
        total = totals.setdefault(item["device_id"], {"kwh": 0.0, "energy_cost": 0.0})
        delta = item["energy_delta_kwh"] or 0.0
        total["kwh"] += delta
        total["energy_cost"] += delta * rate_at(item["captured_at"], config) / 100 * gst_multiplier
    return {
        device_id: {
            "kwh": round(total["kwh"], 3),
            "energy_cost": round(total["energy_cost"], 2),
        }
        for device_id, total in totals.items()
    }


def trend_rows(hours: int, bucket: str, device_id: int | None = None) -> list[dict]:
    """Aggregate raw readings in SQLite for charts and exports."""
    hours = min(max(hours, 1), 24 * 365)
    bucket = {"auto": "minute15" if hours <= 24 else "hour" if hours <= 24 * 7 else "day"}.get(
        bucket, bucket
    )
    bucket_sql = {
        "minute15": "strftime('%Y-%m-%dT%H:', captured_at) || printf('%02d', CAST(CAST(strftime('%M', captured_at) AS INTEGER) / 15 AS INTEGER) * 15) || ':00:00Z'",
        "hour": "strftime('%Y-%m-%dT%H:00:00Z', captured_at)",
        "day": "strftime('%Y-%m-%dT00:00:00Z', captured_at)",
    }.get(bucket)
    if not bucket_sql:
        raise ValueError("bucket must be auto, minute15, hour, or day")
    since = datetime.now(UTC) - timedelta(hours=hours)
    data = _history_rows(since, datetime.now(UTC), device_id)
    grouped: dict[str, dict] = {}
    for item in data:
        timestamp = datetime.fromisoformat(item["captured_at"].replace("Z", "+00:00"))
        if bucket == "minute15":
            timestamp = timestamp.replace(
                minute=(timestamp.minute // 15) * 15, second=0, microsecond=0
            )
        elif bucket == "hour":
            timestamp = timestamp.replace(minute=0, second=0, microsecond=0)
        else:
            timestamp = timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
        key = timestamp.isoformat().replace("+00:00", "Z")
        row = grouped.setdefault(
            key,
            {
                "bucket": key,
                "kwh": 0.0,
                "watts_total": 0.0,
                "watts_count": 0,
                "samples": 0,
            },
        )
        row["kwh"] += item["energy_delta_kwh"] or 0.0
        samples = max(1, int(item.get("samples") or 1))
        if item["watts"] is not None:
            row["watts_total"] += item["watts"] * samples
            row["watts_count"] += samples
        row["samples"] += samples
    return [
        {
            "bucket": row["bucket"],
            "kwh": round(row["kwh"], 4),
            "avg_watts": round(row["watts_total"] / row["watts_count"], 1)
            if row["watts_count"]
            else None,
            "samples": row["samples"],
        }
        for row in grouped.values()
    ]


async def _run_step(name: str, coroutine) -> None:
    """Run one loop step, keeping a failure in it from stopping every other step."""
    try:
        await coroutine
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("%s failed", name)
        open_alert(None, f"internal_{name}", f"Background {name} failed: {exc}")
    else:
        resolve_system_alerts((f"internal_{name}",))


async def polling_loop(poll_lock: asyncio.Lock | None = None) -> None:
    next_archive = datetime.min.replace(tzinfo=UTC)
    next_budget_check = datetime.min.replace(tzinfo=UTC)
    while True:
        await _run_step("schedules", run_due_schedules())
        await _run_step("timers", run_due_timers())
        if poll_lock is None:
            await _run_step("poll", poll_all())
        else:
            async with poll_lock:
                await _run_step("poll", poll_all())
        now = datetime.now(UTC)
        if now >= next_archive:
            await _run_step("archive", asyncio.to_thread(archive_readings))
            next_archive = now + timedelta(hours=1)
        if now >= next_budget_check:
            await _run_step("budget", asyncio.to_thread(check_budget_alerts))
            next_budget_check = now + timedelta(minutes=15)
        try:
            interval = max(3, int(db.settings()["poll_interval_seconds"]))
        except Exception:
            logger.exception("Could not read the poll interval; falling back to 10s")
            interval = 10
        await asyncio.sleep(interval)
