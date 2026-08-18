import json
import os
import sqlite3
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

LEGACY_PREFIX = "GRIDCONNECT"
PREFIX = "SOCKETEER"


def env(name: str, default: str = "") -> str:
    """Read SOCKETEER_<name>, falling back to the pre-rename GRIDCONNECT_<name>.

    Deployments that predate the rename keep working without editing their .env.
    A variable that is set but empty wins over the default, so a deployment can
    switch something off (an optional link, a password) by blanking it.
    """
    for key in (f"{PREFIX}_{name}", f"{LEGACY_PREFIX}_{name}"):
        if key in os.environ:
            return os.environ[key]
    return default


def resolve_db_path(
    default: str = "/data/socketeer.db", legacy: str = "/data/gridconnect.db"
) -> str:
    """Pick the database file, adopting a pre-rename one rather than starting empty.

    Upgrading across the rename must not strand an existing history behind a new,
    empty file, so the old name is used when it is the only one present.
    """
    configured = env("DB")
    if configured:
        return configured
    if not Path(default).exists() and Path(legacy).exists():
        return legacy
    return default


DB_PATH = resolve_db_path()

DEFAULT_SETTINGS = {
    "poll_interval_seconds": 10,
    "timezone": env("TIMEZONE", "Pacific/Auckland") or "Pacific/Auckland",
    "currency": "NZD",
    "tariff_mode": "flat",
    "flat_rate_cents": 30.0,
    "gst_included": True,
    "raw_retention_days": 90,
    "alerts_enabled": True,
    "alert_offline_minutes": 5,
    # A cheap plug run at close to its rating for hours is the failure mode that
    # matters most, so sustained draw is alerted separately from a momentary peak.
    "sustained_load_minutes": 15,
    "sustained_load_percent": 80.0,
    # Nominal supply voltage and the tolerance either side of it. Readings outside
    # this band usually mean a mis-scaled voltage DPS rather than a real supply fault.
    "nominal_voltage": 230.0,
    "voltage_tolerance_percent": 10.0,
    "tou_periods": [
        {
            "name": "Peak",
            "days": [0, 1, 2, 3, 4],
            "start": "07:00",
            "end": "23:00",
            "rate_cents": 35.0,
        },
        {
            "name": "Off-peak",
            "days": [0, 1, 2, 3, 4, 5, 6],
            "start": "23:00",
            "end": "07:00",
            "rate_cents": 22.0,
        },
    ],
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def connection():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def initialise() -> None:
    with connection() as con:
        con.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS devices (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, ip_address TEXT NOT NULL,
          tuya_device_id TEXT NOT NULL, local_key TEXT NOT NULL, protocol_version REAL NOT NULL DEFAULT 3.3,
          tuya_port INTEGER NOT NULL DEFAULT 6668,
          switch_dps TEXT NOT NULL DEFAULT '1', watts_dps TEXT NOT NULL DEFAULT '19', voltage_dps TEXT NOT NULL DEFAULT '20',
          current_dps TEXT NOT NULL DEFAULT '18', energy_dps TEXT NOT NULL DEFAULT '17', power_scale REAL NOT NULL DEFAULT 0.1,
          voltage_scale REAL NOT NULL DEFAULT 0.1, current_scale REAL NOT NULL DEFAULT 0.001, energy_scale REAL NOT NULL DEFAULT 0.01,
          enabled INTEGER NOT NULL DEFAULT 1, monthly_budget_cents REAL, max_watts REAL,
          online INTEGER NOT NULL DEFAULT 0, last_seen TEXT, last_error TEXT, archived_at TEXT
        );
        CREATE TABLE IF NOT EXISTS readings (
          id INTEGER PRIMARY KEY, device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
          captured_at TEXT NOT NULL, is_on INTEGER, watts REAL, voltage REAL, amps REAL, energy_total_kwh REAL, energy_delta_kwh REAL NOT NULL DEFAULT 0,
          UNIQUE(device_id, captured_at)
        );
        CREATE INDEX IF NOT EXISTS readings_device_time ON readings(device_id, captured_at);
        CREATE TABLE IF NOT EXISTS schedules (
          id INTEGER PRIMARY KEY, device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
          days TEXT NOT NULL, at_time TEXT NOT NULL, action TEXT NOT NULL CHECK(action IN ('on','off')), enabled INTEGER NOT NULL DEFAULT 1,
          last_run_date TEXT
        );
        CREATE TABLE IF NOT EXISTS timers (
          id INTEGER PRIMARY KEY, device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
          action TEXT NOT NULL CHECK(action IN ('on','off')), run_at TEXT NOT NULL, label TEXT, created_at TEXT NOT NULL,
          last_error TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
          next_attempt_at TEXT, failed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS timers_run_at ON timers(run_at);
        CREATE TABLE IF NOT EXISTS alerts (
          id INTEGER PRIMARY KEY, device_id INTEGER REFERENCES devices(id) ON DELETE CASCADE,
          kind TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL, resolved_at TEXT
        );
        CREATE INDEX IF NOT EXISTS alerts_open ON alerts(resolved_at, created_at);
        CREATE TABLE IF NOT EXISTS energy_rollups (
          device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
          bucket_start TEXT NOT NULL, bucket_type TEXT NOT NULL CHECK(bucket_type IN ('hour')),
          kwh REAL NOT NULL DEFAULT 0, avg_watts REAL, samples INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(device_id, bucket_start, bucket_type)
        );
        CREATE INDEX IF NOT EXISTS rollups_time ON energy_rollups(bucket_start);
        CREATE TABLE IF NOT EXISTS automation_executions (
          id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('schedule','timer')),
          source_id INTEGER, device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
          device_name TEXT NOT NULL, action TEXT NOT NULL CHECK(action IN ('on','off')),
          scheduled_for TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('success','retrying','failed')),
          attempt INTEGER NOT NULL DEFAULT 1, message TEXT, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS automation_executions_created ON automation_executions(created_at DESC);
        """)
        # Lightweight forward migration for deployments created before relay support.
        columns = {item[1] for item in con.execute("PRAGMA table_info(devices)")}
        if "tuya_port" not in columns:
            con.execute("ALTER TABLE devices ADD COLUMN tuya_port INTEGER NOT NULL DEFAULT 6668")
        if "monthly_budget_cents" not in columns:
            con.execute("ALTER TABLE devices ADD COLUMN monthly_budget_cents REAL")
        if "max_watts" not in columns:
            con.execute("ALTER TABLE devices ADD COLUMN max_watts REAL")
        if "archived_at" not in columns:
            con.execute("ALTER TABLE devices ADD COLUMN archived_at TEXT")
        reading_columns = {item[1] for item in con.execute("PRAGMA table_info(readings)")}
        if "is_on" not in reading_columns:
            con.execute("ALTER TABLE readings ADD COLUMN is_on INTEGER")
        timer_columns = {item[1] for item in con.execute("PRAGMA table_info(timers)")}
        if "attempt_count" not in timer_columns:
            con.execute("ALTER TABLE timers ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0")
        if "next_attempt_at" not in timer_columns:
            con.execute("ALTER TABLE timers ADD COLUMN next_attempt_at TEXT")
        if "failed_at" not in timer_columns:
            con.execute("ALTER TABLE timers ADD COLUMN failed_at TEXT")
        # Older databases may already contain duplicates. Preserve them for an
        # explicit archive/purge decision while the API blocks new duplicates.
        with suppress(sqlite3.IntegrityError):
            con.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS devices_tuya_id_unique ON devices(tuya_device_id)"
            )
        if con.execute("SELECT 1 FROM settings WHERE id=1").fetchone() is None:
            con.execute(
                "INSERT INTO settings(id, value) VALUES(1, ?)",
                (json.dumps(DEFAULT_SETTINGS),),
            )


def settings() -> dict:
    with connection() as con:
        saved = json.loads(con.execute("SELECT value FROM settings WHERE id=1").fetchone()["value"])
    return {key: saved.get(key, default) for key, default in DEFAULT_SETTINGS.items()}


def save_settings(value: dict) -> dict:
    merged = {
        **settings(),
        **{key: item for key, item in value.items() if key in DEFAULT_SETTINGS},
    }
    with connection() as con:
        con.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(merged),))
    return merged


def rows(query: str, parameters=()):
    with connection() as con:
        return [dict(x) for x in con.execute(query, parameters).fetchall()]


def row(query: str, parameters=()):
    result = rows(query, parameters)
    return result[0] if result else None
