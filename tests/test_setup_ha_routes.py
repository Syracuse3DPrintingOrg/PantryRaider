"""The Home Assistant helpers on the Settings page (0.21).

Camera discovery answers each camera with a preview address on this app (an
admin-only route that fetches server-side), never an address with the token
in it. A satellite whose main server relays Home Assistant has no connection
of its own, so both helpers answer 409, and the Status page reports the
server's answer from the last sync.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import ha_client  # noqa: E402

TOKEN = "ha-long-lived-token-routes"
STATES = [
    {"entity_id": "light.kitchen", "attributes": {"friendly_name": "Kitchen"}},
    {"entity_id": "camera.front_door", "attributes": {"friendly_name": "Front Door"}},
    {"entity_id": "camera.garage", "attributes": {}},
]
RELAY = {"v": 1, "caps": ["ai", "recipes_external", "ha", "camera"], "ha_available": True}


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "http://ha.example:8123", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", TOKEN, raising=False)
    with patch.object(type(settings), "is_configured", lambda self: True):
        yield TestClient(app, client=("192.168.1.20", 50000))


@pytest.fixture
def states(monkeypatch):
    calls = []

    async def fake(base=None, token=None, client=None):
        calls.append((base, token))
        return list(STATES)
    monkeypatch.setattr(ha_client, "list_states", fake)
    return calls


def _relay_satellite(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "fleet_relay", dict(RELAY), raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "", raising=False)


def test_camera_discovery_gives_preview_paths_and_no_token(client, states):
    r = client.post("/setup/ha/cameras", json={})
    body = r.json()
    assert body["ok"] is True
    assert body["cameras"] == [
        {"entity_id": "camera.front_door", "name": "Front Door",
         "snapshot_url": "ui/camera/preview?entity=camera.front_door", "stream_url": ""},
        {"entity_id": "camera.garage", "name": "Garage",
         "snapshot_url": "ui/camera/preview?entity=camera.garage", "stream_url": ""},
    ]
    assert TOKEN not in r.text
    assert states == [("http://ha.example:8123", TOKEN)]


def test_camera_discovery_uses_typed_overrides(client, states):
    r = client.post("/setup/ha/cameras", json={"base_url": "http://ha2.example:8123/",
                                               "token": "typed-token"})
    assert r.json()["ok"] is True
    assert states == [("http://ha2.example:8123", "typed-token")]
    assert "typed-token" not in r.text


def test_camera_discovery_without_an_answer(client, monkeypatch):
    async def empty(base=None, token=None, client=None):
        return []
    monkeypatch.setattr(ha_client, "list_states", empty)
    body = client.post("/setup/ha/cameras", json={}).json()
    assert body["ok"] is False and "token" in body["error"]


def test_relay_satellite_answers_409(client, monkeypatch, states):
    _relay_satellite(monkeypatch)
    r = client.post("/setup/ha/cameras", json={})
    assert r.status_code == 409
    assert r.json() == {"ok": False, "error": "Home Assistant is set up on the main server."}
    r = client.get("/setup/ha/entities")
    assert r.status_code == 409
    assert states == []


def test_entities_need_a_direct_connection(client, monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_ha_token", "", raising=False)
    body = client.get("/setup/ha/entities").json()
    assert body["configured"] is False and body["groups"] == []


def test_status_summary_reports_home_assistant_on_a_relay_satellite(client, monkeypatch):
    _relay_satellite(monkeypatch)
    items = client.get("/setup/status/summary").json()["items"]
    assert "home_assistant" in items
    assert items["home_assistant"]["state"] in ("ok", "good")
    monkeypatch.setattr(settings, "fleet_relay", dict(RELAY, ha_available=False), raising=False)
    items = client.get("/setup/status/summary").json()["items"]
    assert "home_assistant" not in items


@pytest.mark.anyio
async def test_ha_probe_modes(monkeypatch):
    from app.routers import setup as setup_router

    async def pong():
        return True
    monkeypatch.setattr(ha_client, "ping", pong)
    monkeypatch.setattr(ha_client, "mode", lambda: "direct")
    assert await setup_router._ha_probe() == {"configured": True, "ok": True}
    monkeypatch.setattr(ha_client, "mode", lambda: "upstream")
    monkeypatch.setattr(settings, "fleet_relay", dict(RELAY), raising=False)
    assert await setup_router._ha_probe() == {"configured": True, "ok": True}
    monkeypatch.setattr(ha_client, "mode", lambda: "none")
    assert await setup_router._ha_probe() == {"configured": False, "ok": False}
