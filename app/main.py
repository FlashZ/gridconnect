import asyncio
import csv
import io
import os
import secrets
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.background import BackgroundTask

from . import __version__ as VERSION
from . import database as db
from .database import DB_PATH
from .services import (
    archive_readings,
    check_budget_alerts,
    cloud_devices,
    control_device,
    detect_protocol,
    device_summaries,
    live_power_total,
    mark_poll_failure,
    persist_reading,
    poll_with_retry,
    polling_loop,
    read_raw_dps,
    reading_is_stale,
    summary,
    test_lan_endpoint,
    trend_rows,
)

STATIC = Path(__file__).parent / "static"

DEFAULT_PROJECT_URL = "https://github.com/FlashZ/gridconnect"
DEFAULT_SUPPORT_URL = "https://buymeacoffee.com/nickkb"


def _safe_url(value: str) -> str:
    """Allow only http(s) links. These end up as hrefs in the dashboard footer."""
    value = (value or "").strip()
    return value if value.startswith(("http://", "https://")) else ""


def about_links() -> dict:
    """Footer identity, overridable so a fork points at its own project and funding."""
    project = _safe_url(os.getenv("GRIDCONNECT_PROJECT_URL", DEFAULT_PROJECT_URL))
    licence = _safe_url(
        os.getenv("GRIDCONNECT_LICENSE_URL")
        or (f"{project.rstrip('/')}/blob/main/LICENSE" if project else "")
    )
    return {
        "name": os.getenv("GRIDCONNECT_PROJECT_NAME", "GridConnect").strip()[:60] or "GridConnect",
        "version": VERSION,
        "project_url": project,
        "license_url": licence,
        # A fork sets this to an empty string to drop the link entirely, rather
        # than soliciting on the upstream author's behalf.
        "support_url": _safe_url(os.getenv("GRIDCONNECT_SUPPORT_URL", DEFAULT_SUPPORT_URL)),
    }


def describe(exc: Exception) -> str:
    """Render an exception for the UI. Several, notably TimeoutError, stringify empty."""
    if isinstance(exc, TimeoutError | asyncio.TimeoutError):
        return "the plug did not reply within 30 seconds"
    return str(exc) or exc.__class__.__name__


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.initialise()
    app.state.poll_lock = asyncio.Lock()
    task = asyncio.create_task(polling_loop(app.state.poll_lock))
    yield
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


app = FastAPI(title="GridConnect", version=VERSION, lifespan=lifespan)


@app.middleware("http")
async def optional_basic_auth(request: Request, call_next):
    """Keep LAN access frictionless by default; opt into Basic auth through .env."""
    password = os.getenv("GRIDCONNECT_AUTH_PASSWORD")
    if not password or request.url.path in {
        "/api/health",
        "/manifest.webmanifest",
        "/sw.js",
        "/icon.svg",
    }:
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    expected = (
        "Basic " + __import__("base64").b64encode(f"gridconnect:{password}".encode()).decode()
    )
    if not secrets.compare_digest(auth, expected):
        return Response(
            "Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="GridConnect"'},
        )
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        expected_origin = (
            f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"
        )
        if origin and origin.rstrip("/") != expected_origin.rstrip("/"):
            return Response("Cross-origin request rejected", status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TouPeriod(StrictModel):
    name: str = Field(min_length=1, max_length=60)
    days: list[int] = Field(min_length=1, max_length=7)
    start: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    rate_cents: float = Field(ge=0, le=1000)

    @field_validator("days")
    @classmethod
    def valid_days(cls, value: list[int]) -> list[int]:
        if any(day not in range(7) for day in value) or len(set(value)) != len(value):
            raise ValueError("days must contain unique values from 0 to 6")
        return value


class SettingsIn(StrictModel):
    poll_interval_seconds: int = Field(ge=3, le=3600)
    timezone: str
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    tariff_mode: str = Field(pattern=r"^(flat|time_of_use)$")
    flat_rate_cents: float = Field(ge=0, le=1000)
    gst_included: bool
    raw_retention_days: int = Field(ge=1, le=3650)
    alerts_enabled: bool
    alert_offline_minutes: int = Field(ge=0, le=10080)
    sustained_load_minutes: int = Field(default=15, ge=0, le=1440)
    sustained_load_percent: float = Field(default=80.0, ge=0, le=100)
    nominal_voltage: float = Field(default=230.0, ge=0, le=600)
    voltage_tolerance_percent: float = Field(default=10.0, ge=0, le=100)
    tou_periods: list[TouPeriod] = Field(max_length=48)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except Exception as exc:
            raise ValueError("timezone must be an IANA name, e.g. Pacific/Auckland") from exc
        return value

    @model_validator(mode="after")
    def tou_requires_periods(self):
        if self.tariff_mode == "time_of_use" and not self.tou_periods:
            raise ValueError("peak/off-peak pricing requires at least one period")
        return self


class DeviceIn(StrictModel):
    name: str
    ip_address: str
    tuya_device_id: str
    local_key: str
    protocol_version: float = 3.3
    tuya_port: int = Field(default=6668, ge=1, le=65535)
    switch_dps: str = "1"
    watts_dps: str = "19"
    voltage_dps: str = "20"
    current_dps: str = "18"
    energy_dps: str = "17"
    power_scale: float = 0.1
    voltage_scale: float = 0.1
    current_scale: float = 0.001
    energy_scale: float = 0.01
    enabled: bool = True
    monthly_budget_cents: float | None = Field(default=None, ge=0)
    max_watts: float | None = Field(default=None, ge=0)


class DevicePatch(StrictModel):
    # Accepted and ignored: the edit form posts the whole record back, including the
    # row id from its hidden field. Rejecting it made every save fail with a 422.
    id: int | None = None
    name: str | None = None
    ip_address: str | None = None
    tuya_device_id: str | None = None
    local_key: str | None = None
    protocol_version: float | None = None
    tuya_port: int | None = Field(default=None, ge=1, le=65535)
    switch_dps: str | None = None
    watts_dps: str | None = None
    voltage_dps: str | None = None
    current_dps: str | None = None
    energy_dps: str | None = None
    power_scale: float | None = None
    voltage_scale: float | None = None
    current_scale: float | None = None
    energy_scale: float | None = None
    enabled: bool | None = None
    monthly_budget_cents: float | None = Field(default=None, ge=0)
    max_watts: float | None = Field(default=None, ge=0)


class ScheduleIn(StrictModel):
    device_id: int
    days: list[int] = Field(min_length=1, max_length=7)
    at_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    action: str = Field(pattern=r"^(on|off)$")
    enabled: bool = True

    @field_validator("days")
    @classmethod
    def valid_days(cls, value: list[int]) -> list[int]:
        if any(day not in range(7) for day in value) or len(set(value)) != len(value):
            raise ValueError("days must contain unique values from 0 to 6")
        return value


class CloudSetupIn(StrictModel):
    api_region: str = Field(pattern=r"^(cn|us|us-e|eu|eu-w|sg|in)$")
    api_key: str = Field(min_length=1)
    api_secret: str = Field(min_length=1)
    api_device_id: str | None = None


class LanTestIn(StrictModel):
    ip_address: str
    tuya_port: int = Field(default=6668, ge=1, le=65535)


class ProtocolDetectIn(LanTestIn):
    tuya_device_id: str = Field(min_length=1)
    local_key: str = Field(min_length=1)


class TimerIn(StrictModel):
    device_id: int
    action: str = Field(pattern=r"^(on|off)$")
    run_at: datetime
    label: str | None = Field(default=None, max_length=120)

    @field_validator("run_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("run_at must include a timezone offset")
        return value


@app.get("/")
def dashboard():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/icon.svg")
def icon():
    return FileResponse(
        STATIC / "icon.svg",
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/sw.js")
def service_worker():
    return FileResponse(
        STATIC / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/health")
def health():
    devices_online = db.row(
        "SELECT COUNT(*) AS count FROM devices WHERE online=1 AND archived_at IS NULL"
    )["count"]
    devices_total = db.row(
        "SELECT COUNT(*) AS count FROM devices WHERE enabled=1 AND archived_at IS NULL"
    )["count"]
    return {
        "ok": True,
        "status": "healthy" if devices_online == devices_total else "degraded",
        "service": "gridconnect",
        "version": VERSION,
        "devices_online": devices_online,
        "devices_total": devices_total,
        "timestamp": db.utc_now(),
    }


@app.get("/api/health/devices")
def device_health_summary():
    return {
        **health(),
        "devices": db.rows(
            """SELECT id,name,online,last_seen,last_error FROM devices
               WHERE enabled=1 AND archived_at IS NULL ORDER BY name"""
        ),
    }


@app.get("/api/widgets/summary")
def widget_summary():
    config = db.settings()
    now = datetime.now(ZoneInfo(config["timezone"]))
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    total = summary(today, now)
    return {
        "watts": live_power_total(),
        "today_kwh": total["kwh"],
        "today_cost": total["energy_cost"],
        "currency": config["currency"],
        **health(),
    }


@app.get("/api/about")
def about():
    """Project identity for the dashboard footer. Configurable so forks can rebrand."""
    return about_links()


@app.get("/api/settings")
def get_settings():
    return db.settings()


@app.put("/api/settings")
def put_settings(value: SettingsIn):
    return db.save_settings(value.model_dump())


@app.get("/api/devices")
def get_devices(include_archived: bool = False):
    where = "" if include_archived else "WHERE archived_at IS NULL"
    return db.rows(
        f"""SELECT id,name,ip_address,tuya_device_id,protocol_version,tuya_port,
                   switch_dps,watts_dps,voltage_dps,current_dps,energy_dps,
                   power_scale,voltage_scale,current_scale,energy_scale,enabled,
                   monthly_budget_cents,max_watts,online,last_seen,last_error,archived_at
            FROM devices {where} ORDER BY archived_at IS NOT NULL,name"""
    )


@app.post("/api/setup/test-lan")
async def test_lan(payload: LanTestIn):
    try:
        reachable = await asyncio.to_thread(
            test_lan_endpoint, payload.ip_address, payload.tuya_port
        )
    except OSError:
        reachable = False
    return {
        "ip_address": payload.ip_address,
        "tuya_port": payload.tuya_port,
        "tuya_local_port_reachable": reachable,
    }


@app.post("/api/setup/cloud-devices")
async def get_cloud_devices(payload: CloudSetupIn):
    try:
        return await asyncio.to_thread(cloud_devices, **payload.model_dump())
    except Exception as exc:
        raise HTTPException(
            502, f"Could not retrieve linked Tuya devices: {describe(exc)}"
        ) from exc


@app.post("/api/setup/detect-protocol")
async def protocol_detect(payload: ProtocolDetectIn):
    try:
        return await asyncio.to_thread(detect_protocol, **payload.model_dump())
    except Exception as exc:
        raise HTTPException(
            502, f"Could not detect a working Tuya protocol: {describe(exc)}"
        ) from exc


@app.post("/api/devices")
def create_device(device: DeviceIn):
    if db.row("SELECT id FROM devices WHERE tuya_device_id=?", (device.tuya_device_id,)):
        raise HTTPException(409, "That Tuya device is already configured")
    fields = list(device.model_dump())
    values = [int(v) if isinstance(v, bool) else v for v in device.model_dump().values()]
    try:
        with db.connection() as con:
            cursor = con.execute(
                f"INSERT INTO devices({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
                values,
            )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "That Tuya device is already configured") from exc
    return {"id": cursor.lastrowid}


@app.patch("/api/devices/{device_id}")
def update_device(device_id: int, device: DevicePatch):
    if not db.row("SELECT id FROM devices WHERE id=?", (device_id,)):
        raise HTTPException(404, "Device not found")
    changes = device.model_dump(exclude_unset=True)
    changes.pop("id", None)
    if changes.get("local_key") == "":
        changes.pop("local_key")
    if not changes:
        return {"ok": True}
    if "tuya_device_id" in changes and db.row(
        "SELECT id FROM devices WHERE tuya_device_id=? AND id<>?",
        (changes["tuya_device_id"], device_id),
    ):
        raise HTTPException(409, "That Tuya device is already configured")
    values = [int(v) if isinstance(v, bool) else v for v in changes.values()]
    try:
        with db.connection() as con:
            con.execute(
                f"UPDATE devices SET {','.join(f'{x}=?' for x in changes)} WHERE id=?",
                values + [device_id],
            )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "That Tuya device is already configured") from exc
    return {"ok": True}


@app.post("/api/devices/{device_id}/archive")
def archive_device(device_id: int, archived: bool = True):
    if not db.row("SELECT id FROM devices WHERE id=?", (device_id,)):
        raise HTTPException(404, "Device not found")
    with db.connection() as con:
        con.execute(
            "UPDATE devices SET archived_at=?,enabled=? WHERE id=?",
            (db.utc_now() if archived else None, 0 if archived else 1, device_id),
        )
    return {"ok": True, "archived": archived}


@app.delete("/api/devices/{device_id}")
def delete_device(device_id: int, confirm: str = ""):
    if confirm != "PURGE":
        raise HTTPException(422, "Type PURGE to permanently delete this device and its history")
    device = db.row("SELECT archived_at FROM devices WHERE id=?", (device_id,))
    if not device:
        raise HTTPException(404, "Device not found")
    if not device["archived_at"]:
        raise HTTPException(409, "Archive the device before permanently deleting it")
    with db.connection() as con:
        con.execute("DELETE FROM devices WHERE id=?", (device_id,))
    return {"ok": True}


@app.post("/api/devices/{device_id}/power")
async def set_power(device_id: int, state: bool):
    device = db.row("SELECT * FROM devices WHERE id=?", (device_id,))
    if not device:
        raise HTTPException(404, "Device not found")
    try:
        await asyncio.to_thread(control_device, device, state)
    except Exception as exc:
        raise HTTPException(502, describe(exc)) from exc
    return {"ok": True, "state": state}


@app.get("/api/devices/{device_id}/health")
def device_health(device_id: int):
    device = db.row(
        """SELECT id,name,ip_address,protocol_version,tuya_port,enabled,online,last_seen,last_error
                       FROM devices WHERE id=?""",
        (device_id,),
    )
    if not device:
        raise HTTPException(404, "Device not found")
    device["last_reading"] = db.row(
        """SELECT captured_at,is_on,watts,voltage,amps,energy_total_kwh
                                      FROM readings WHERE device_id=? ORDER BY captured_at DESC LIMIT 1""",
        (device_id,),
    )
    return device


@app.post("/api/devices/{device_id}/dps")
async def inspect_dps(device_id: int):
    """Read a plug's raw DPS channels so a wrong metering mapping can be corrected."""
    device = db.row("SELECT * FROM devices WHERE id=?", (device_id,))
    if not device:
        raise HTTPException(404, "Device not found")
    try:
        async with app.state.poll_lock:
            return await asyncio.wait_for(asyncio.to_thread(read_raw_dps, device), timeout=30)
    except Exception as exc:
        raise HTTPException(502, f"Could not read DPS channels: {describe(exc)}") from exc


@app.post("/api/devices/{device_id}/test")
async def test_device(device_id: int):
    device = db.row("SELECT * FROM devices WHERE id=?", (device_id,))
    if not device:
        raise HTTPException(404, "Device not found")
    started = perf_counter()
    try:
        async with app.state.poll_lock:
            reading = await asyncio.wait_for(asyncio.to_thread(poll_with_retry, device), timeout=30)
            await asyncio.to_thread(persist_reading, device_id, reading)
    except Exception as exc:
        mark_poll_failure(device, exc, "Connection test failed: ")
        raise HTTPException(502, f"Connection test failed: {describe(exc)}") from exc
    return {
        "ok": True,
        "response_ms": round((perf_counter() - started) * 1000),
        "reading": reading,
    }


@app.get("/api/schedules")
def get_schedules():
    return db.rows(
        """SELECT s.*,d.name AS device_name FROM schedules s JOIN devices d ON d.id=s.device_id
           WHERE d.archived_at IS NULL ORDER BY at_time"""
    )


@app.post("/api/schedules")
def create_schedule(schedule: ScheduleIn):
    if not db.row(
        "SELECT id FROM devices WHERE id=? AND archived_at IS NULL",
        (schedule.device_id,),
    ):
        raise HTTPException(404, "Device not found")
    with db.connection() as con:
        cursor = con.execute(
            "INSERT INTO schedules(device_id,days,at_time,action,enabled) VALUES(?,?,?,?,?)",
            (
                schedule.device_id,
                ",".join(map(str, schedule.days)),
                schedule.at_time,
                schedule.action,
                int(schedule.enabled),
            ),
        )
    return {"id": cursor.lastrowid}


@app.delete("/api/schedules/{schedule_id}")
def delete_schedule(schedule_id: int):
    with db.connection() as con:
        con.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
    return {"ok": True}


@app.get("/api/timers")
def get_timers():
    return db.rows(
        """SELECT t.*,d.name AS device_name FROM timers t JOIN devices d ON d.id=t.device_id
           WHERE d.archived_at IS NULL ORDER BY run_at"""
    )


@app.post("/api/timers")
def create_timer(timer: TimerIn):
    if timer.run_at.astimezone(UTC) <= datetime.now(UTC):
        raise HTTPException(422, "Timer must be in the future")
    if not db.row("SELECT id FROM devices WHERE id=? AND archived_at IS NULL", (timer.device_id,)):
        raise HTTPException(404, "Device not found")
    with db.connection() as con:
        cursor = con.execute(
            "INSERT INTO timers(device_id,action,run_at,label,created_at) VALUES(?,?,?,?,?)",
            (
                timer.device_id,
                timer.action,
                timer.run_at.astimezone(UTC).isoformat(),
                timer.label,
                db.utc_now(),
            ),
        )
    return {"id": cursor.lastrowid}


@app.delete("/api/timers/{timer_id}")
def delete_timer(timer_id: int):
    with db.connection() as con:
        con.execute("DELETE FROM timers WHERE id=?", (timer_id,))
    return {"ok": True}


@app.get("/api/automation-executions")
def automation_executions(limit: int = 50):
    return db.rows(
        "SELECT * FROM automation_executions ORDER BY created_at DESC LIMIT ?",
        (min(max(limit, 1), 500),),
    )


@app.get("/api/alerts")
def get_alerts(include_resolved: bool = False):
    where = "" if include_resolved else "WHERE a.resolved_at IS NULL"
    return db.rows(
        f"SELECT a.*,d.name AS device_name FROM alerts a LEFT JOIN devices d ON d.id=a.device_id {where} ORDER BY a.created_at DESC LIMIT 100"
    )


@app.post("/api/alerts/{alert_id}/resolve")
def resolve_alert(alert_id: int):
    with db.connection() as con:
        con.execute("UPDATE alerts SET resolved_at=? WHERE id=?", (db.utc_now(), alert_id))
    return {"ok": True}


@app.post("/api/maintenance/archive")
async def archive_history():
    return {"raw_readings_archived": await asyncio.to_thread(archive_readings)}


@app.post("/api/maintenance/check-budgets")
async def check_budgets():
    """Re-evaluate budget alerts now instead of waiting for the next 15-minute sweep."""
    await asyncio.to_thread(check_budget_alerts)
    return {"ok": True}


@app.get("/api/backup")
def backup_database():
    if not Path(DB_PATH).exists():
        raise HTTPException(404, "No database exists yet")
    handle, path = tempfile.mkstemp(prefix="gridconnect-backup-", suffix=".db")
    os.close(handle)
    with sqlite3.connect(DB_PATH) as source, sqlite3.connect(path) as destination:
        source.backup(destination)
    return FileResponse(
        path,
        media_type="application/vnd.sqlite3",
        filename="gridconnect-backup.db",
        background=BackgroundTask(os.unlink, path),
    )


@app.post("/api/backup/restore")
async def restore_database(request: Request, confirm: str = ""):
    """Validate and atomically restore SQLite while polling is paused."""
    if confirm != "RESTORE":
        raise HTTPException(422, "Type RESTORE to confirm database replacement")
    directory = Path(DB_PATH).parent
    directory.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix="gridconnect-restore-", suffix=".db", dir=directory)
    total = 0
    try:
        with os.fdopen(handle, "wb") as output:
            async for chunk in request.stream():
                total += len(chunk)
                if total > 1_000_000_000:
                    raise HTTPException(413, "Backup must be under 1 GB")
                output.write(chunk)
        if total == 0:
            raise HTTPException(422, "Backup must be a non-empty SQLite file")
        with sqlite3.connect(temporary) as candidate:
            tables = {
                row[0]
                for row in candidate.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            integrity = candidate.execute("PRAGMA integrity_check").fetchone()[0]
        if not {"devices", "readings", "settings"}.issubset(tables) or integrity != "ok":
            raise HTTPException(422, "That file is not a valid GridConnect backup")
        async with app.state.poll_lock:
            with db.connection() as current:
                current.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            os.replace(temporary, DB_PATH)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{DB_PATH}{suffix}")
                if sidecar.exists():
                    sidecar.unlink()
            db.initialise()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, f"Could not restore backup: {exc}") from exc
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"ok": True, "restart_required": False}


@app.get("/api/overview")
def overview():
    config = db.settings()
    now = datetime.now(ZoneInfo(config["timezone"]))
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = today - timedelta(days=today.weekday())
    month = today.replace(day=1)
    devices = db.rows("""SELECT d.id,d.name,d.ip_address,d.protocol_version,d.tuya_port,d.monthly_budget_cents,d.max_watts,d.enabled,d.online,d.last_seen,d.last_error,r.is_on,r.watts,r.voltage,r.amps,r.captured_at
      FROM devices d LEFT JOIN readings r ON r.id=(SELECT id FROM readings WHERE device_id=d.id ORDER BY captured_at DESC LIMIT 1)
      WHERE d.archived_at IS NULL ORDER BY d.name""")
    per_period = {
        "today": device_summaries(today, now),
        "week": device_summaries(week, now),
        "month": device_summaries(month, now),
    }
    for device in devices:
        device["usage"] = {
            period: totals.get(device["id"], {"kwh": 0.0, "energy_cost": 0.0})
            for period, totals in per_period.items()
        }
        # Flag readings the plug has not refreshed, so the dashboard can show them
        # as last-known rather than current.
        device["reading_stale"] = reading_is_stale(device.get("captured_at"), config)
    alerts = get_alerts()
    return {
        "devices": devices,
        "live_watts": live_power_total(),
        "today": summary(today, now),
        "week": summary(week, now),
        "month": summary(month, now),
        "alerts": alerts,
        "currency": config["currency"],
        "server_time": db.utc_now(),
    }


@app.get("/api/trends")
def trends(hours: int = 24, bucket: str = "auto", device_id: int | None = None):
    try:
        return trend_rows(hours, bucket, device_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/trends.csv")
def trends_csv(hours: int = 24, bucket: str = "auto", device_id: int | None = None):
    try:
        rows = trend_rows(hours, bucket, device_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["bucket", "kwh", "avg_watts", "samples"])
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=gridconnect-trends.csv"},
    )
