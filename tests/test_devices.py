"""Tests for the satellite device registry (services/devices.py) and the
/api/config/satellite heartbeat path that populates it."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

# Isolated in-memory DB so tests never touch the real data directory.
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database import Base
from app.models import db_models  # noqa: F401 - registers all models with Base

_test_engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
_TestSession = sessionmaker(bind=_test_engine)
Base.metadata.create_all(bind=_test_engine)


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch):
    """Point the registry at an in-memory DB for every test."""
    from app.services import devices
    monkeypatch.setattr(devices, "SessionLocal", _TestSession)
    # Wipe between tests.
    db = _TestSession()
    from app.models.db_models import SatelliteDevice
    db.query(SatelliteDevice).delete()
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# record_heartbeat
# ---------------------------------------------------------------------------

def test_heartbeat_creates_device():
    from app.services import devices
    cmd = devices.record_heartbeat("dev-aaa", hostname="pi-1", ip="10.0.0.5",
                                   deployment_mode="pi_remote", version="1.5.0")
    assert cmd is None  # nothing queued
    rows = devices.list_devices()
    assert len(rows) == 1
    d = rows[0]
    assert d["device_id"] == "dev-aaa"
    assert d["hostname"] == "pi-1"
    assert d["ip"] == "10.0.0.5"
    assert d["version"] == "1.5.0"
    assert d["online"] is True  # just registered = fresh


def test_heartbeat_updates_existing():
    from app.services import devices
    devices.record_heartbeat("dev-bbb", hostname="pi-2", ip="10.0.0.6")
    devices.record_heartbeat("dev-bbb", hostname="pi-2-renamed", ip="10.0.0.7")
    rows = devices.list_devices()
    assert len(rows) == 1
    assert rows[0]["hostname"] == "pi-2-renamed"
    assert rows[0]["ip"] == "10.0.0.7"


def test_heartbeat_drains_command():
    from app.services import devices
    devices.record_heartbeat("dev-ccc")
    devices.queue_command("dev-ccc", "resync")

    # First heartbeat: command returned and cleared.
    cmd = devices.record_heartbeat("dev-ccc")
    assert cmd == "resync"

    # Second heartbeat: queue is empty.
    cmd2 = devices.record_heartbeat("dev-ccc")
    assert cmd2 is None


def test_heartbeat_ignores_empty_device_id():
    from app.services import devices
    result = devices.record_heartbeat("")
    assert result is None
    assert devices.list_devices() == []


# ---------------------------------------------------------------------------
# queue_command
# ---------------------------------------------------------------------------

def test_queue_command_rejects_unknown():
    from app.services import devices
    devices.record_heartbeat("dev-ddd")
    assert devices.queue_command("dev-ddd", "unknown_command") is False


def test_queue_command_unknown_device():
    from app.services import devices
    assert devices.queue_command("nonexistent", "resync") is False


def test_queue_command_known():
    from app.services import devices
    devices.record_heartbeat("dev-eee")
    assert devices.queue_command("dev-eee", "resync") is True


# ---------------------------------------------------------------------------
# online flag
# ---------------------------------------------------------------------------

def test_online_flag_old_device(monkeypatch):
    """A device last seen more than ONLINE_WINDOW_SECONDS ago reads as offline."""
    from app.services import devices
    from app.models.db_models import SatelliteDevice
    from datetime import datetime, timezone, timedelta

    devices.record_heartbeat("dev-fff", ip="10.0.0.9")
    # Back-date last_seen well beyond the window.
    db = _TestSession()
    dev = db.query(SatelliteDevice).filter_by(device_id="dev-fff").first()
    old = (datetime.now(timezone.utc) - timedelta(seconds=devices.ONLINE_WINDOW_SECONDS + 60))
    dev.last_seen = old.isoformat(timespec="seconds")
    db.commit()
    db.close()

    rows = devices.list_devices()
    assert rows[0]["online"] is False


# ---------------------------------------------------------------------------
# label / forget / scan results
# ---------------------------------------------------------------------------

def test_set_label():
    from app.services import devices
    devices.record_heartbeat("dev-ggg")
    assert devices.set_label("dev-ggg", "Kitchen Pi") is True
    assert devices.list_devices()[0]["label"] == "Kitchen Pi"


def test_forget_device():
    from app.services import devices
    devices.record_heartbeat("dev-hhh")
    assert devices.forget_device("dev-hhh") is True
    assert devices.list_devices() == []


def test_forget_nonexistent():
    from app.services import devices
    assert devices.forget_device("no-such") is False


def test_scan_result_does_not_shadow_heartbeat():
    """record_scan_result should skip an IP that already has a heartbeat row."""
    from app.services import devices
    devices.record_heartbeat("dev-iii", ip="10.0.0.20")
    devices.record_scan_result("10.0.0.20", hostname="scan-ghost", version="9.9.9")
    rows = devices.list_devices()
    assert len(rows) == 1
    assert rows[0]["device_id"] == "dev-iii"
    assert rows[0]["version"] != "9.9.9"


def test_scan_result_creates_synthetic_row():
    from app.services import devices
    devices.record_scan_result("10.0.0.30", hostname="unknown-box", version="1.4.0")
    rows = devices.list_devices()
    assert len(rows) == 1
    assert rows[0]["device_id"] == "scan:10.0.0.30"
    assert rows[0]["source"] == "scan"


# ---------------------------------------------------------------------------
# Satellite endpoint heartbeat via TestClient (monkeypatched registry)
# ---------------------------------------------------------------------------

def test_satellite_config_records_heartbeat(monkeypatch):
    """GET /api/config/satellite with identity headers should record a device
    and return the command key in the response payload.

    We monkeypatch record_heartbeat so this test does not need the full DB
    stack; it proves the router wires identity headers to the registry and
    includes the returned command in the JSON body.
    """
    import os
    import tempfile
    os.environ["DATA_DIR"] = tempfile.mkdtemp()

    from app.main import app
    from app.config import settings
    from app.services import devices as dev_svc
    from fastapi.testclient import TestClient

    object.__setattr__(settings, "api_key", "test-server-key")
    object.__setattr__(settings, "deployment_mode", "server")

    heartbeat_calls = []

    def fake_heartbeat(device_id, *, hostname=None, ip=None,
                       deployment_mode=None, version=None):
        heartbeat_calls.append({
            "device_id": device_id, "hostname": hostname,
            "ip": ip, "version": version,
        })
        return "resync" if len(heartbeat_calls) == 1 else None

    monkeypatch.setattr(dev_svc, "record_heartbeat", fake_heartbeat)

    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/api/config/satellite", headers={
        "X-API-Key": "test-server-key",
        "X-Device-Id": "pi-x1",
        "X-Device-Hostname": "kitchen-pi",
        "X-Device-Mode": "pi_remote",
        "X-Device-Version": "1.6.0",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "config" in data
    assert data["command"] == "resync"
    assert heartbeat_calls[0]["hostname"] == "kitchen-pi"

    # Second call: fake_heartbeat returns None, response must echo that.
    resp2 = client.get("/api/config/satellite", headers={
        "X-API-Key": "test-server-key",
        "X-Device-Id": "pi-x1",
    })
    assert resp2.json()["command"] is None


# ---------------------------------------------------------------------------
# lan_cidr_from_known_devices (blank-scan fallback for bridge-only servers)
# ---------------------------------------------------------------------------

def test_lan_cidr_prefers_real_lan_heartbeat():
    from app.services import devices
    # A Docker-network scan row and a real-LAN heartbeat row.
    devices.record_scan_result("172.19.0.1", version="0.7.7")
    devices.record_heartbeat("dev-pi", ip="192.168.1.31", deployment_mode="pi_remote")
    assert devices.lan_cidr_from_known_devices() == "192.168.1.0/24"


def test_lan_cidr_skips_docker_and_loopback():
    from app.services import devices
    devices.record_scan_result("172.19.0.1", version="0.7.7")  # docker only
    assert devices.lan_cidr_from_known_devices() is None


# ---------------------------------------------------------------------------
# Scan CIDR resolution: derive the LAN from configured backend URLs
# ---------------------------------------------------------------------------

def test_scan_cidr_from_grocy_url(monkeypatch):
    from app.config import settings
    from app.routers import devices as router
    monkeypatch.setattr(settings, "grocy_base_url", "http://192.168.1.170:9383", raising=False)
    monkeypatch.setattr(settings, "mealie_base_url", "", raising=False)
    monkeypatch.setattr(settings, "grocy_public_url", "", raising=False)
    monkeypatch.setattr(settings, "mealie_public_url", "", raising=False)
    assert router._lan_cidr_from_config_urls() == "192.168.1.0/24"


def test_scan_cidr_skips_docker_and_servicename(monkeypatch):
    from app.config import settings
    from app.routers import devices as router
    for f in ("grocy_base_url", "mealie_base_url", "grocy_public_url", "mealie_public_url"):
        monkeypatch.setattr(settings, f, "", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy:80", raising=False)
    monkeypatch.setattr(settings, "mealie_base_url", "http://172.19.0.5:9285", raising=False)
    assert router._lan_cidr_from_config_urls() is None


def test_resolve_prefers_explicit(monkeypatch):
    from app.routers import devices as router
    assert router._resolve_scan_cidr("10.0.5.0/24") == "10.0.5.0/24"
