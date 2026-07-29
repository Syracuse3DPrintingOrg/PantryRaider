"""Tests for deployment-mode selection in the setup wizard.

Covers the mode taxonomy (Server hosted / Pi Hosted / Pi Remote), host-aware
filtering of the offered modes, persistence, and the relaxed is_configured()
rule for the thin-client Pi Remote mode.

The wizard reads Pi-ness through app.routers.setup.is_raspberry_pi /
board_model; we patch those bound names directly so the tests run on any host
without touching /proc/device-tree or fighting lru_cache.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.routers import setup as setup_router  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

# Settings keys this suite mutates; reset to a clean baseline around each test.
_RESET = {
    "deployment_mode": "",
    "remote_server_url": "",
    "grocy_base_url": "http://grocy:80",
    "grocy_api_key": "",
    "auth_required": True,
    "auth_password": "",
}


@pytest.fixture
def client(monkeypatch, tmp_path):
    # Templates load from the relative path "app/templates", so the app must run
    # with the working directory set to service/ (matches the container).
    cwd = os.getcwd()
    os.chdir(SERVICE)
    # Persist settings to a throwaway dir so save() never touches real data.
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    for k, v in _RESET.items():
        monkeypatch.setattr(settings, k, v, raising=False)
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _as_pi(monkeypatch, model="Raspberry Pi 5 Model B"):
    monkeypatch.setattr(setup_router, "is_raspberry_pi", lambda: True)
    monkeypatch.setattr(setup_router, "board_model", lambda: model)


def _as_server(monkeypatch):
    monkeypatch.setattr(setup_router, "is_raspberry_pi", lambda: False)
    monkeypatch.setattr(setup_router, "board_model", lambda: "")


def test_pi_offers_pi_modes_hides_server(client, monkeypatch):
    _as_pi(monkeypatch)
    html = client.get("/setup").text
    # Assert on the rendered mode cards (the mode names also appear in JS
    # comments, so check the card element IDs that only the card loop emits).
    assert 'id="mode-pi_hosted"' in html
    assert 'id="mode-pi_remote"' in html
    assert 'id="mode-server"' not in html


def test_low_ram_pi_restricts_to_pi_remote(client, monkeypatch):
    # A weak Pi (low board tier / low RAM) drops Pi Hosted: the local stack
    # cannot run, so only the thin-client Pi Remote mode is offered.
    _as_pi(monkeypatch, "Raspberry Pi 3 Model B")
    monkeypatch.setattr(setup_router, "supports_local_stack", lambda: False)
    html = client.get("/setup").text
    assert 'id="mode-pi_remote"' in html
    assert 'id="mode-pi_hosted"' not in html
    assert 'id="mode-server"' not in html
    # The wizard explains why Pi Hosted is gone.
    assert "Pi Hosted is hidden" in html


def test_capable_pi_offers_both_pi_modes(client, monkeypatch):
    _as_pi(monkeypatch, "Raspberry Pi 5 Model B")
    monkeypatch.setattr(setup_router, "supports_local_stack", lambda: True)
    html = client.get("/setup").text
    assert 'id="mode-pi_hosted"' in html
    assert 'id="mode-pi_remote"' in html
    assert "Pi Hosted is hidden" not in html


def test_uncertain_pi_detection_keeps_both_modes(client, monkeypatch):
    # supports_local_stack defaults True on an uncertain reading, so a real Pi
    # whose model could not be classified (unknown tier) and whose RAM is
    # unreadable still sees both Pi modes rather than being over-restricted.
    from app import hardware
    _as_pi(monkeypatch, "Raspberry Pi")
    monkeypatch.setattr(hardware, "board_model", lambda: "Raspberry Pi")
    monkeypatch.setattr(hardware, "total_ram_mb", lambda: None)
    html = client.get("/setup").text
    assert 'id="mode-pi_hosted"' in html
    assert 'id="mode-pi_remote"' in html


def test_non_pi_offers_server_only(client, monkeypatch):
    _as_server(monkeypatch)
    html = client.get("/setup").text
    assert 'id="mode-server"' in html
    assert 'id="mode-pi_hosted"' not in html
    assert 'id="mode-pi_remote"' not in html


def test_save_mode_persists_and_strips_slash(client, monkeypatch):
    _as_pi(monkeypatch)
    r = client.post("/setup/mode", json={
        "deployment_mode": "pi_remote",
        "remote_server_url": "http://192.168.1.50:9284/",
    })
    assert r.json() == {"ok": True, "mode": "pi_remote"}
    assert settings.deployment_mode == "pi_remote"
    assert settings.remote_server_url == "http://192.168.1.50:9284"


def test_unknown_mode_rejected(client, monkeypatch):
    _as_pi(monkeypatch)
    r = client.post("/setup/mode", json={"deployment_mode": "nonsense"})
    assert r.json()["ok"] is False


def test_remote_mode_configured_without_grocy(client, monkeypatch):
    _as_pi(monkeypatch, "Raspberry Pi 3 Model B")
    client.post("/setup/mode", json={
        "deployment_mode": "pi_remote",
        "remote_server_url": "http://server:9284",
    })
    # A satellite pulls Grocy/AI from its server, so no local Grocy is needed.
    # It does need the upstream API key (to authenticate the pull) and the
    # usual password gate.
    settings.save({"auth_password": "secret"})
    assert settings.is_configured() is False   # still missing the upstream key
    settings.save({"upstream_api_key": "shared-key"})
    assert settings.is_configured() is True


def test_remote_mode_needs_url(client, monkeypatch):
    _as_pi(monkeypatch, "Raspberry Pi 3 Model B")
    settings.save({"auth_password": "secret", "deployment_mode": "pi_remote",
                   "upstream_api_key": "shared-key", "remote_server_url": ""})
    assert settings.is_configured() is False


def test_test_remote_handles_unreachable(client, monkeypatch):
    _as_pi(monkeypatch)
    r = client.post("/setup/test/remote", json={"remote_server_url": "http://127.0.0.1:1"})
    assert r.status_code == 200
    assert r.json()["ok"] is False


# Home Assistant camera discovery (FoodAssistant-cr50) ------------------------

class _FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Async context-manager stand-in returning a canned /api/states response."""

    def __init__(self, resp):
        self._resp = resp

    def __init_subclass__(cls):  # pragma: no cover - not subclassed
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kwargs):
        return self._resp


def _patch_ha(monkeypatch, resp):
    import httpx
    monkeypatch.setattr(setup_router, "httpx", httpx, raising=False)
    monkeypatch.setattr(
        setup_router.httpx, "AsyncClient", lambda *a, **k: _FakeAsyncClient(resp)
    )


def test_ha_discover_needs_credentials(client, monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "", raising=False)
    r = client.post("/setup/ha/cameras", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "url and token" in body["error"].lower()


def test_ha_discover_lists_camera_entities(client, monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "http://ha.local:8123", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "tok", raising=False)
    states = [
        {"entity_id": "light.kitchen", "attributes": {"friendly_name": "Kitchen"}},
        {"entity_id": "camera.front_door", "attributes": {"friendly_name": "Front Door"}},
        {"entity_id": "camera.garage", "attributes": {}},
    ]
    _patch_ha(monkeypatch, _FakeResp(200, states))
    r = client.post("/setup/ha/cameras", json={})
    body = r.json()
    assert body["ok"] is True
    cams = body["cameras"]
    # Only camera.* entities, sorted by name, with built URLs and derived names.
    assert [c["entity_id"] for c in cams] == ["camera.front_door", "camera.garage"]
    assert cams[1]["name"] == "Garage"  # derived from entity id when no friendly_name
    assert cams[0]["snapshot_url"] == (
        "http://ha.local:8123/api/camera_proxy/camera.front_door?token=tok"
    )
    assert cams[0]["stream_url"].endswith("/api/camera_proxy_stream/camera.front_door?token=tok")


def test_ha_discover_reports_bad_token(client, monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "http://ha.local:8123", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "tok", raising=False)
    _patch_ha(monkeypatch, _FakeResp(401, {}))
    r = client.post("/setup/ha/cameras", json={})
    body = r.json()
    assert body["ok"] is False
    assert "401" in body["error"] or "token" in body["error"].lower()
