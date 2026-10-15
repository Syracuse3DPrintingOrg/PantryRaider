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
from sqlalchemy.pool import StaticPool
from app.database import Base
from app.models import db_models  # noqa: F401 - registers all models with Base

# One shared connection, so a route running in the server's worker thread sees
# the same in-memory database as the test.
_test_engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                             poolclass=StaticPool)
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

def test_satellite_config_records_heartbeat(monkeypatch, tmp_path):
    """GET /api/config/satellite with identity headers should record a device
    and return the command key in the response payload.

    We monkeypatch record_heartbeat so this test does not need the full DB
    stack; it proves the router wires identity headers to the registry and
    includes the returned command in the JSON body.
    """
    from app.main import app
    from app.config import settings
    from app.services import devices as dev_svc
    from fastapi.testclient import TestClient

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "api_key", "test-server-key")
    monkeypatch.setattr(settings, "deployment_mode", "server")

    heartbeat_calls = []

    def fake_heartbeat(device_id, *, hostname=None, ip=None,
                       deployment_mode=None, version=None, **_new_fields):
        heartbeat_calls.append({
            "device_id": device_id, "hostname": hostname,
            "ip": ip, "version": version,
        })
        return "resync" if len(heartbeat_calls) == 1 else None

    monkeypatch.setattr(dev_svc, "record_heartbeat", fake_heartbeat)

    client = TestClient(app, raise_server_exceptions=True, client=("192.168.1.20", 50000))
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
# 0.21: the address a pull came from, and what the device reports
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("peer, reported, forwarded, expected", [
    # A LAN peer is the device itself: its own report is not needed.
    ("192.168.1.31", "10.9.9.9", False, ("192.168.1.31", False)),
    ("::ffff:192.168.1.31", "", False, ("192.168.1.31", False)),
    ("fd00::31", "", False, ("fd00::31", False)),
    # A pull over IPv6 from a device that names its IPv4 LAN address: that
    # address fits the http://<ip>/ links the integration and Devices open.
    ("fe80::31", "192.168.1.31", False, ("192.168.1.31", True)),
    ("fd00::31", "192.168.1.31", False, ("192.168.1.31", True)),
    ("fd00::31", "203.0.113.9", False, ("fd00::31", False)),
    # Loopback and the Docker bridge are never the device: take a private report.
    ("127.0.0.1", "192.168.1.31", False, ("192.168.1.31", True)),
    ("172.17.0.1", "192.168.1.31", False, ("192.168.1.31", True)),
    ("172.17.0.1", "fe80::1", False, ("fe80::1", True)),
    # A report that is not a private LAN address is never taken.
    ("172.17.0.1", "203.0.113.9", False, ("172.17.0.1", True)),
    ("127.0.0.1", "", False, ("127.0.0.1", True)),
    # Through a reverse proxy on the LAN, the peer is the proxy.
    ("192.168.1.2", "192.168.1.31", True, ("192.168.1.31", True)),
    ("192.168.1.2", "", True, ("192.168.1.2", True)),
    ("192.168.1.2", "not-an-ip", True, ("192.168.1.2", True)),
    # Garbage in, never an exception.
    ("testclient", "garbage", False, ("testclient", True)),
    ("", "", False, ("", True)),
    (None, None, False, ("", True)),
])
def test_observed_ip_matrix(peer, reported, forwarded, expected):
    from app.services import devices
    assert devices.observed_ip(peer, reported, forwarded=forwarded) == expected


def test_heartbeat_new_fields_round_trip():
    from app.services import devices
    devices.record_heartbeat("dev-new", ip="192.168.1.31", deployment_mode="pi_remote",
                             caps=["relay1", "deckproxy1"], client="satellite/0.21.0",
                             peer="192.168.1.31", reported_ip="192.168.1.31",
                             trust="SHA256:abc", ip_unverified=False)
    d = devices.list_devices()[0]
    assert d["caps"] == ["deckproxy1", "relay1"]
    assert d["relay"] is True and d["deck_proxy"] is True
    assert d["client"] == "satellite/0.21.0"
    assert d["trust_fp"] == "SHA256:abc"
    assert d["ip_unverified"] is False
    # A later heartbeat from an older caller leaves the 0.21 fields alone.
    devices.record_heartbeat("dev-new", hostname="renamed")
    d = devices.list_devices()[0]
    assert d["relay"] is True and d["trust_fp"] == "SHA256:abc"
    # Only relay1 now.
    devices.record_heartbeat("dev-new", caps=["relay1"], ip_unverified=True)
    d = devices.list_devices()[0]
    assert d["relay"] is True and d["deck_proxy"] is False and d["ip_unverified"] is True


def test_older_rows_read_as_not_reported():
    from app.services import devices
    devices.record_heartbeat("dev-old", ip="192.168.1.40")
    d = devices.list_devices()[0]
    assert d["caps"] == [] and d["relay"] is False and d["deck_proxy"] is False
    assert d["client"] == "" and d["trust_fp"] == "" and d["ip_unverified"] is False
    # Every key the Devices page already read is still there.
    for key in ("device_id", "hostname", "ip", "deployment_mode", "version",
                "update_status", "version_diff", "label", "source",
                "pending_command", "first_seen", "last_seen", "online"):
        assert key in d


def _pull(client, headers):
    base = {"X-API-Key": "test-server-key", "X-Device-Id": "pi-fwd",
            "X-Device-Mode": "pi_remote", "X-Device-Version": "0.21.0"}
    base.update(headers)
    return client.get("/api/config/satellite", headers=base)


def test_pull_records_observed_peer_caps_client_and_trust(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.config import settings
    from app.services import devices
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "api_key", "test-server-key")
    monkeypatch.setattr(settings, "auth_password", "")
    client = TestClient(app, client=("192.168.1.20", 50000))
    r = _pull(client, {"X-Device-Ip": "10.66.66.66", "X-PR-Caps": "relay1",
                       "X-PR-Client": "satellite/0.21.0", "X-Device-Trust": "SHA256:xyz"})
    assert r.status_code == 200
    d = devices.list_devices()[0]
    # The reported address is not taken over the address the pull came from.
    assert d["ip"] == "192.168.1.20" and d["ip_unverified"] is False
    assert d["relay"] is True and d["client"] == "satellite/0.21.0"
    assert d["trust_fp"] == "SHA256:xyz"


def test_pull_through_a_proxy_records_the_reported_address(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.config import settings
    from app.services import devices
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "api_key", "test-server-key")
    monkeypatch.setattr(settings, "auth_password", "")
    client = TestClient(app, client=("192.168.1.2", 50000))
    r = _pull(client, {"X-Device-Ip": "192.168.1.31",
                       "X-Forwarded-For": "192.168.1.31"})
    assert r.status_code == 200
    d = devices.list_devices()[0]
    assert d["ip"] == "192.168.1.31"
    assert d["ip_unverified"] is True


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


def test_lan_cidr_skips_a_device_recorded_at_an_ipv6_address():
    from app.services import devices
    # A pull over IPv6 records the address it came from; it has no /24.
    devices.record_heartbeat("dev-v6", ip="fd00::31", deployment_mode="pi_remote")
    assert devices.lan_cidr_from_known_devices() is None
    devices.record_heartbeat("dev-pi", ip="192.168.1.31", deployment_mode="pi_remote")
    assert devices.lan_cidr_from_known_devices() == "192.168.1.0/24"


# ---------------------------------------------------------------------------
# Scan CIDR resolution: derive the LAN from configured backend URLs
# ---------------------------------------------------------------------------

def test_scan_cidr_from_grocy_url(monkeypatch):
    from app.config import settings
    from app.routers import devices as router
    monkeypatch.setattr(settings, "grocy_base_url", "http://192.168.1.60:9383", raising=False)
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


# ---------------------------------------------------------------------------
# The LAN sweep only ever probes the ports the app answers on
# ---------------------------------------------------------------------------

def test_scan_ignores_a_caller_supplied_port_list(monkeypatch):
    """No UI caller passes ports, and honouring an arbitrary list would turn
    the sweep into a general port scanner aimed at the user's own network."""
    import asyncio
    from app.config import settings
    from app.routers import devices as router
    from app.services import lan_scan

    seen = {}

    def fake_scan(cidr, ports=None, **kw):
        seen["cidr"], seen["ports"] = cidr, ports
        return []

    monkeypatch.setattr(lan_scan, "scan_for_instances", fake_scan)
    monkeypatch.setattr(settings, "lan_scan_cidr", "10.0.5.0/24", raising=False)
    monkeypatch.setattr(settings, "device_id", "self", raising=False)
    body = router.ScanBody(cidr="10.0.5.0/24", ports=[22, 3389, 5900])
    out = asyncio.run(router.scan_lan(body))
    assert out["ok"] is True
    assert seen["ports"] == list(lan_scan.DEFAULT_PORTS)


def test_unreadable_versions_never_break_the_device_list():
    # A version is untrusted text: from a key holder's X-Device-Version, or
    # from any host's /health during a LAN scan. Latin-1 superscript digits
    # pass str.isdigit() but make int() raise, and thousands of digits pass
    # the digit check but hit int()'s limit. Neither may break the list.
    from app.models.db_models import SatelliteDevice
    from app.services import devices
    devices.record_heartbeat("dev-sup", ip="10.0.0.40", version="1.²")
    devices.record_scan_result("10.0.0.41", version="9" * 5000)
    devices.record_heartbeat("dev-ok", ip="10.0.0.42", version="0.20.3")
    # A row stored before this check, holding a bad value directly.
    db = devices.SessionLocal()
    try:
        db.add(SatelliteDevice(device_id="dev-old", first_seen="2026-01-01T00:00:00+00:00",
                               source="heartbeat", version="2.³" + "1" * 5000))
        db.commit()
    finally:
        db.close()
    rows = {r["device_id"]: r for r in devices.list_devices()}
    assert rows["dev-sup"]["version"] is None
    assert rows["dev-sup"]["update_status"] == "unknown"
    assert rows["scan:10.0.0.41"]["version"] is None
    assert rows["dev-old"]["version"] is None
    assert rows["dev-ok"]["version"] == "0.20.3"
