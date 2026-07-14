"""Tests for the Home Assistant integration API (FoodAssistant-ju93):
GET /ha/state and POST /ha/settings.

Covers mode shaping (server/pi_hosted include the fleet-hub fields, pi_remote
omits them), auth behaviour through the existing require_auth middleware
(valid key accepted, missing key rejected when a password is set, open install
needs no key), graceful degradation when the host bridge or Grocy are
unreachable, and the settings-write validation/apply path. No network or
Docker: GrocyClient, gadgets, the bridge, and the device registry are all
monkeypatched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    # A non-default Grocy URL makes is_configured() True so requests are not
    # bounced to the /setup wizard by the setup-redirect middleware.
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    # Auth off by default; individual tests opt in by setting auth_password
    # (which also implies is_configured() no longer needs auth_required off).
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "api_key", "", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", [], raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr("app.hardware.is_raspberry_pi", lambda: False)
    # The /expiring/count TTLCache this router reuses is a process-global
    # singleton, so a value cached by an earlier test would otherwise leak in.
    from app.routers import expiring as expiring_router
    expiring_router._count_items_cache.invalidate()
    with TestClient(app) as c:
        yield c
    expiring_router._count_items_cache.invalidate()


def _empty_grocy(monkeypatch):
    """No Grocy backend needed: return an empty expiring list."""
    from app.services.grocy import GrocyClient

    async def fake_get_expiring(self, days=7):
        return []
    monkeypatch.setattr(GrocyClient, "get_expiring", fake_get_expiring)


def _empty_gadgets(monkeypatch):
    from app.services import gadgets
    monkeypatch.setattr(gadgets, "get_state", lambda: {"devices": []})


def _empty_devices(monkeypatch):
    from app.services import devices as devices_svc
    monkeypatch.setattr(devices_svc, "list_devices", lambda: [])


def _empty_printing(monkeypatch):
    from app.services import printing as printing_svc
    monkeypatch.setattr(printing_svc, "list_queues", lambda: [])
    monkeypatch.setattr(printing_svc, "local_label_queue", lambda choice: "")
    monkeypatch.setattr(printing_svc, "resolve_effective_queue", lambda local, inherited: "")


def _quiet_backends(monkeypatch):
    """Stub every optional backend to empty so a test can focus on shape."""
    _empty_grocy(monkeypatch)
    _empty_gadgets(monkeypatch)
    _empty_devices(monkeypatch)
    _empty_printing(monkeypatch)


# ---------------------------------------------------------------------------
# Mode shaping
# ---------------------------------------------------------------------------

def test_server_mode_includes_fleet_fields(client, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    _quiet_backends(monkeypatch)

    resp = client.get("/ha/state")
    assert resp.status_code == 200
    data = resp.json()

    assert data["app"] == "pantryraider"
    assert data["mode"] == "server"
    for key in ("display", "presence", "printers"):
        assert key in data
    for key in ("expiring", "counts", "timers", "thermometers", "satellites"):
        assert key in data


def test_pi_hosted_mode_includes_fleet_fields(client, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted", raising=False)
    _quiet_backends(monkeypatch)

    resp = client.get("/ha/state")
    data = resp.json()
    assert data["mode"] == "pi_hosted"
    for key in ("expiring", "counts", "timers", "thermometers", "satellites"):
        assert key in data


def test_pi_remote_mode_omits_fleet_fields_keeps_common(client, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    # A satellite's is_configured() needs the upstream link instead of a
    # local Grocy URL.
    monkeypatch.setattr(settings, "remote_server_url", "http://server.test", raising=False)
    monkeypatch.setattr(settings, "upstream_api_key", "upstream-key", raising=False)
    _quiet_backends(monkeypatch)

    resp = client.get("/ha/state")
    assert resp.status_code == 200
    data = resp.json()

    assert data["mode"] == "pi_remote"
    for key in ("display", "presence", "printers", "app", "version",
                "device_id", "hostname"):
        assert key in data
    for key in ("expiring", "counts", "timers", "thermometers", "satellites"):
        assert key not in data


def test_empty_mode_reads_as_server(client, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "", raising=False)
    _quiet_backends(monkeypatch)

    resp = client.get("/ha/state")
    data = resp.json()
    assert data["mode"] == "server"
    assert "expiring" in data


# ---------------------------------------------------------------------------
# Auth behaviour
# ---------------------------------------------------------------------------

def test_open_install_needs_no_key(client, monkeypatch):
    _quiet_backends(monkeypatch)
    resp = client.get("/ha/state")
    assert resp.status_code == 200


def test_auth_on_with_valid_key_succeeds(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_password", "hunter2", raising=False)
    monkeypatch.setattr(settings, "api_key", "test-key", raising=False)
    _quiet_backends(monkeypatch)

    resp = client.get("/ha/state", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200


def test_auth_on_without_key_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_password", "hunter2", raising=False)
    monkeypatch.setattr(settings, "api_key", "test-key", raising=False)
    _quiet_backends(monkeypatch)

    resp = client.get("/ha/state")
    assert resp.status_code == 401


def test_auth_on_with_wrong_key_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_password", "hunter2", raising=False)
    monkeypatch.setattr(settings, "api_key", "test-key", raising=False)
    _quiet_backends(monkeypatch)

    resp = client.get("/ha/state", headers={"X-API-Key": "wrong-key"})
    assert resp.status_code == 401


def test_settings_auth_on_with_valid_key_succeeds(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_password", "hunter2", raising=False)
    monkeypatch.setattr(settings, "api_key", "test-key", raising=False)

    resp = client.post("/ha/settings", json={"display_idle_timeout": 10},
                       headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200


def test_settings_auth_on_without_key_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_password", "hunter2", raising=False)
    monkeypatch.setattr(settings, "api_key", "test-key", raising=False)

    resp = client.post("/ha/settings", json={"display_idle_timeout": 10})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------

def test_presence_degrades_when_bridge_unreachable(client, monkeypatch):
    """On a Pi, a bridge that cannot be reached degrades to unavailable, not
    a 500."""
    monkeypatch.setattr("app.hardware.is_raspberry_pi", lambda: True)
    monkeypatch.setattr("app.routers.ha.is_raspberry_pi", lambda: True)
    _quiet_backends(monkeypatch)

    resp = client.get("/ha/state")
    assert resp.status_code == 200
    assert resp.json()["presence"] == {"available": False, "detected": False}


def test_presence_off_pi_skips_bridge_call(client, monkeypatch):
    """Off a Pi the bridge is never dialled; presence is just the calm default."""
    called = []

    async def _boom(*a, **kw):
        called.append(True)
        raise AssertionError("bridge should not be called off a Pi")

    monkeypatch.setattr("app.routers.ha.is_raspberry_pi", lambda: False)
    monkeypatch.setattr("app.services.bridge.bridge_client", _boom)
    _quiet_backends(monkeypatch)

    resp = client.get("/ha/state")
    assert resp.status_code == 200
    assert resp.json()["presence"] == {"available": False, "detected": False}
    assert called == []


def test_expiring_degrades_on_grocy_error(client, monkeypatch):
    from app.services.grocy import GrocyClient, GrocyError

    async def fake_get_expiring(self, days=7):
        raise GrocyError("unreachable")
    monkeypatch.setattr(GrocyClient, "get_expiring", fake_get_expiring)
    _empty_gadgets(monkeypatch)
    _empty_devices(monkeypatch)
    _empty_printing(monkeypatch)

    resp = client.get("/ha/state")
    assert resp.status_code == 200
    expiring = resp.json()["expiring"]
    assert expiring == {"expired": 0, "today": 0, "within_3_days": 0,
                        "within_7_days": 0, "expiring_ok": False}


def test_printers_degrade_on_backend_failure(client, monkeypatch):
    from app.services import printing as printing_svc

    def _boom():
        raise RuntimeError("no cups")
    monkeypatch.setattr(printing_svc, "list_queues", _boom)
    _empty_grocy(monkeypatch)
    _empty_gadgets(monkeypatch)
    _empty_devices(monkeypatch)

    resp = client.get("/ha/state")
    assert resp.status_code == 200
    printers = resp.json()["printers"]
    assert printers["queues"] == []


def test_thermometers_degrade_on_gadgets_failure(client, monkeypatch):
    from app.services import gadgets

    def _boom():
        raise RuntimeError("state file unreadable")
    monkeypatch.setattr(gadgets, "get_state", _boom)
    _empty_grocy(monkeypatch)
    _empty_devices(monkeypatch)
    _empty_printing(monkeypatch)

    resp = client.get("/ha/state")
    assert resp.status_code == 200
    assert resp.json()["thermometers"] == []


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------

def test_thermometers_trimmed_shape(client, monkeypatch):
    from app.services import gadgets

    def fake_state():
        return {"devices": [{
            "id": "AA:BB", "name": "Grill", "battery": 71, "stale": False,
            "rssi": -50, "age_seconds": 3.2,
            "probes": [{"index": 0, "temp_c": 62.5, "role": "food",
                        "role_label": "Food", "role_source": "auto",
                        "target_c": 63.0, "direction": "above",
                        "device_target_c": None, "ready_in_seconds": 40}],
        }]}
    monkeypatch.setattr(gadgets, "get_state", fake_state)
    _empty_grocy(monkeypatch)
    _empty_devices(monkeypatch)
    _empty_printing(monkeypatch)

    resp = client.get("/ha/state")
    thermos = resp.json()["thermometers"]
    assert thermos == [{
        "id": "AA:BB", "name": "Grill", "battery": 71, "stale": False,
        "probes": [{"index": 0, "role": "food", "role_label": "Food",
                    "temp_c": 62.5, "target_c": 63.0}],
    }]


def test_satellites_shape(client, monkeypatch):
    from app.services import devices as devices_svc

    def fake_list_devices():
        return [{"device_id": "pi-1", "hostname": "kitchen-pi", "ip": "10.0.0.5",
                 "version": "0.18.1", "last_seen": "2026-07-14T00:00:00+00:00",
                 "online": True, "label": None, "source": "heartbeat"}]
    monkeypatch.setattr(devices_svc, "list_devices", fake_list_devices)
    _empty_grocy(monkeypatch)
    _empty_gadgets(monkeypatch)
    _empty_printing(monkeypatch)

    resp = client.get("/ha/state")
    assert resp.json()["satellites"] == [{
        "device_id": "pi-1", "hostname": "kitchen-pi", "ip": "10.0.0.5",
        "version": "0.18.1", "last_seen": "2026-07-14T00:00:00+00:00",
    }]


def test_timers_next_and_running(client, monkeypatch):
    from app.services import timers as timers_svc

    def fake_list_timers():
        return [
            {"label": "Pasta", "remaining_seconds": 120.0, "expired": False},
            {"label": "Bread", "remaining_seconds": 30.0, "expired": False},
            {"label": "Done one", "remaining_seconds": 0.0, "expired": True},
        ]
    monkeypatch.setattr(timers_svc, "list_timers", fake_list_timers)
    _empty_grocy(monkeypatch)
    _empty_gadgets(monkeypatch)
    _empty_devices(monkeypatch)
    _empty_printing(monkeypatch)

    resp = client.get("/ha/state")
    timers = resp.json()["timers"]
    assert timers["running"] == 2
    assert timers["next"] == {"label": "Bread", "remaining_seconds": 30.0}


def test_timers_next_none_when_nothing_running(client, monkeypatch):
    from app.services import timers as timers_svc
    monkeypatch.setattr(timers_svc, "list_timers", lambda: [])
    _quiet_backends(monkeypatch)

    resp = client.get("/ha/state")
    timers = resp.json()["timers"]
    assert timers == {"running": 0, "next": None}


# ---------------------------------------------------------------------------
# POST /ha/settings
# ---------------------------------------------------------------------------

def test_settings_write_happy_path(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)

    resp = client.post("/ha/settings", json={
        "display_idle_timeout": 15,
        "screensaver_minutes": 5,
        "screensaver_mode": "toasters",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert set(body["applied"]) == {
        "display_idle_timeout", "screensaver_minutes", "screensaver_mode"}
    assert settings.display_idle_timeout == 15
    assert settings.screensaver_minutes == 5
    assert settings.screensaver_mode == "toasters"


def test_settings_write_applies_only_provided_keys(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "screensaver_minutes", 7, raising=False)

    resp = client.post("/ha/settings", json={"display_idle_timeout": 20})
    assert resp.status_code == 200
    assert resp.json()["applied"] == ["display_idle_timeout"]
    assert settings.display_idle_timeout == 20
    assert settings.screensaver_minutes == 7  # untouched


def test_settings_write_empty_body_applies_nothing(client):
    resp = client.post("/ha/settings", json={})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "applied": []}


def test_settings_rejects_idle_timeout_out_of_range(client):
    resp = client.post("/ha/settings", json={"display_idle_timeout": 999})
    assert resp.status_code == 422


def test_settings_rejects_negative_idle_timeout(client):
    resp = client.post("/ha/settings", json={"display_idle_timeout": -1})
    assert resp.status_code == 422


def test_settings_rejects_bad_screensaver_mode(client):
    resp = client.post("/ha/settings", json={"screensaver_mode": "not-a-mode"})
    assert resp.status_code == 422


def test_settings_accepts_boundary_values(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    resp = client.post("/ha/settings", json={
        "display_idle_timeout": 0, "screensaver_minutes": 120})
    assert resp.status_code == 200
    assert settings.display_idle_timeout == 0
    assert settings.screensaver_minutes == 120


@pytest.mark.parametrize("value", ["auto", "on", "off"])
def test_settings_wake_on_presence_accepted_values(client, monkeypatch, tmp_path, value):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    resp = client.post("/ha/settings", json={"wake_on_presence": value})
    assert resp.status_code == 200
    assert resp.json()["applied"] == ["wake_on_presence"]


def test_settings_rejects_bad_wake_on_presence(client):
    resp = client.post("/ha/settings", json={"wake_on_presence": "sometimes"})
    assert resp.status_code == 422


def test_settings_push_display_idle_called_on_relevant_change(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    calls = []

    async def fake_push():
        calls.append(True)
        return True
    monkeypatch.setattr("app.routers.setup._push_display_idle", fake_push)

    resp = client.post("/ha/settings", json={"display_idle_timeout": 10})
    assert resp.status_code == 200
    assert calls == [True]


def test_settings_push_display_idle_not_called_for_unrelated_change(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    calls = []

    async def fake_push():
        calls.append(True)
        return True
    monkeypatch.setattr("app.routers.setup._push_display_idle", fake_push)

    resp = client.post("/ha/settings", json={"screensaver_minutes": 5})
    assert resp.status_code == 200
    assert calls == []
