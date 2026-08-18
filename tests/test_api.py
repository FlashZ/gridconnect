"""HTTP-level tests, covering the contract the dashboard actually depends on."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import database as db
from app import main


@pytest.fixture(autouse=True)
def temporary_database(tmp_path, monkeypatch):
    path = tmp_path / "socketeer.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    monkeypatch.setattr(main, "DB_PATH", str(path))
    db.initialise()
    return path


@pytest.fixture
def client(monkeypatch):
    """A client with the background polling loop stubbed out."""

    async def no_polling(*_args, **_kwargs):
        return None

    monkeypatch.setattr(main, "polling_loop", no_polling)
    with TestClient(main.app) as test_client:
        yield test_client


def make_device(client, **overrides):
    payload = {
        "name": "EV charger",
        "ip_address": "192.168.1.42",
        "tuya_device_id": "eb3ea270b5dfd9b6373wrc",
        "local_key": "0123456789abcdef",
        **overrides,
    }
    response = client.post("/api/devices", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_health_reports_service_and_version(client):
    body = client.get("/api/health").json()
    assert body["service"] == "socketeer"
    assert body["version"]


def test_patch_accepts_the_id_the_edit_form_posts_back(client):
    """The dashboard posts the whole record, including its hidden id field."""
    device_id = make_device(client)
    response = client.patch(
        f"/api/devices/{device_id}",
        json={"id": device_id, "name": "Garage charger", "max_watts": 2400},
    )
    assert response.status_code == 200, response.text
    device = client.get("/api/devices").json()[0]
    assert device["name"] == "Garage charger"
    assert device["max_watts"] == 2400


def test_patch_still_rejects_unknown_fields(client):
    device_id = make_device(client)
    response = client.patch(f"/api/devices/{device_id}", json={"colour": "red"})
    assert response.status_code == 422


def test_patch_can_set_every_metering_channel(client):
    """The edit dialog exposes voltage and current mapping, so the API must accept it."""
    device_id = make_device(client)
    mapping = {
        "watts_dps": "19",
        "voltage_dps": "20",
        "current_dps": "18",
        "energy_dps": "17",
        "power_scale": 0.1,
        "voltage_scale": 0.01,
        "current_scale": 0.001,
        "energy_scale": 0.01,
    }
    assert client.patch(f"/api/devices/{device_id}", json=mapping).status_code == 200
    device = client.get("/api/devices").json()[0]
    for key, value in mapping.items():
        assert device[key] == value


def test_overview_excludes_stale_readings_from_live_load(client):
    device_id = make_device(client)
    with db.connection() as con:
        con.execute(
            """INSERT INTO readings(device_id,captured_at,is_on,watts,voltage,amps,
               energy_total_kwh,energy_delta_kwh) VALUES(?,?,1,900,230,4,1,0)""",
            (device_id, (datetime.now(UTC) - timedelta(hours=3)).isoformat()),
        )
        con.execute("UPDATE devices SET online=0 WHERE id=?", (device_id,))
    body = client.get("/api/overview").json()
    assert body["live_watts"] == 0
    assert body["devices"][0]["reading_stale"] is True
    assert body["devices"][0]["watts"] == 900, "the last known value is still exposed"


def test_settings_round_trip_includes_the_safety_thresholds(client):
    settings = client.get("/api/settings").json()
    settings.update(
        {
            "sustained_load_minutes": 20,
            "sustained_load_percent": 75,
            "nominal_voltage": 230,
            "voltage_tolerance_percent": 6,
        }
    )
    response = client.put("/api/settings", json=settings)
    assert response.status_code == 200, response.text
    assert response.json()["sustained_load_minutes"] == 20
    assert client.get("/api/settings").json()["voltage_tolerance_percent"] == 6


def test_purge_requires_archiving_and_the_confirmation_word(client):
    device_id = make_device(client)
    assert client.delete(f"/api/devices/{device_id}").status_code == 422
    assert client.delete(f"/api/devices/{device_id}?confirm=PURGE").status_code == 409
    client.post(f"/api/devices/{device_id}/archive?archived=true")
    assert client.delete(f"/api/devices/{device_id}?confirm=PURGE").status_code == 200


def test_duplicate_tuya_device_is_rejected(client):
    make_device(client)
    response = client.post(
        "/api/devices",
        json={
            "name": "Second copy",
            "ip_address": "192.168.1.43",
            "tuya_device_id": "eb3ea270b5dfd9b6373wrc",
            "local_key": "0123456789abcdef",
        },
    )
    assert response.status_code == 409


def test_trends_csv_is_downloadable(client):
    response = client.get("/api/trends.csv?hours=24")
    assert response.status_code == 200
    assert "socketeer-trends.csv" in response.headers["content-disposition"]
    assert response.text.splitlines()[0] == "bucket,kwh,avg_watts,samples"


def test_trends_rejects_an_unknown_bucket(client):
    assert client.get("/api/trends?bucket=fortnight").status_code == 422


def test_timer_must_be_in_the_future(client):
    device_id = make_device(client)
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    response = client.post(
        "/api/timers", json={"device_id": device_id, "action": "off", "run_at": past}
    )
    assert response.status_code == 422


def test_static_assets_the_pwa_needs_are_served(client):
    assert client.get("/manifest.webmanifest").status_code == 200
    assert client.get("/icon.svg").status_code == 200
    assert client.get("/sw.js").status_code == 200


def test_about_uses_project_defaults(client):
    body = client.get("/api/about").json()
    assert body["name"] == "Socketeer"
    assert body["version"]
    assert body["project_url"] == "https://github.com/FlashZ/socketeer"
    assert body["license_url"].endswith("/blob/main/LICENSE")
    assert body["support_url"].startswith("https://")


def test_about_can_be_rebranded_by_a_fork(client, monkeypatch):
    monkeypatch.setenv("SOCKETEER_PROJECT_NAME", "PlugWatch")
    monkeypatch.setenv("SOCKETEER_PROJECT_URL", "https://example.org/plugwatch")
    monkeypatch.setenv("SOCKETEER_SUPPORT_URL", "")
    body = client.get("/api/about").json()
    assert body["name"] == "PlugWatch"
    assert body["project_url"] == "https://example.org/plugwatch"
    assert body["license_url"] == "https://example.org/plugwatch/blob/main/LICENSE"
    assert body["support_url"] == "", "a fork must be able to drop the support link"


def test_about_rejects_a_non_http_url(client, monkeypatch):
    """These values are rendered as hrefs, so only http(s) may survive."""
    monkeypatch.setenv("SOCKETEER_PROJECT_URL", "javascript:alert(1)")
    monkeypatch.setenv("SOCKETEER_SUPPORT_URL", "data:text/html,<script>1</script>")
    body = client.get("/api/about").json()
    assert body["project_url"] == ""
    assert body["support_url"] == ""


def test_legacy_env_vars_still_work(monkeypatch):
    """A deployment predating the rename must keep working untouched."""
    monkeypatch.delenv("SOCKETEER_PROJECT_NAME", raising=False)
    monkeypatch.setenv("GRIDCONNECT_PROJECT_NAME", "Legacy name")
    assert db.env("PROJECT_NAME", "Socketeer") == "Legacy name"


def test_new_env_var_wins_over_the_legacy_one(monkeypatch):
    monkeypatch.setenv("GRIDCONNECT_PROJECT_NAME", "Old")
    monkeypatch.setenv("SOCKETEER_PROJECT_NAME", "New")
    assert db.env("PROJECT_NAME", "Socketeer") == "New"


def test_an_explicitly_empty_value_beats_the_default(monkeypatch):
    monkeypatch.setenv("SOCKETEER_SUPPORT_URL", "")
    assert db.env("SUPPORT_URL", "https://example.org") == ""


def test_a_pre_rename_database_is_adopted_rather_than_ignored(tmp_path, monkeypatch):
    """Renaming must not strand an existing history behind a fresh empty file."""
    monkeypatch.delenv("SOCKETEER_DB", raising=False)
    monkeypatch.delenv("GRIDCONNECT_DB", raising=False)
    # A directory of its own: the shared fixture already puts a socketeer.db in tmp_path.
    data = tmp_path / "migration"
    data.mkdir()
    new, legacy = data / "socketeer.db", data / "gridconnect.db"
    legacy.write_bytes(b"")
    assert db.resolve_db_path(str(new), str(legacy)) == str(legacy)

    # Once the new file exists it wins, and an explicit setting always wins.
    new.write_bytes(b"")
    assert db.resolve_db_path(str(new), str(legacy)) == str(new)
    monkeypatch.setenv("SOCKETEER_DB", "/tmp/chosen.db")
    assert db.resolve_db_path(str(new), str(legacy)) == "/tmp/chosen.db"
