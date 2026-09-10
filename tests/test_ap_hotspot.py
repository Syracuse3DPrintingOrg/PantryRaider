"""The app-side recovery-hotspot config: /setup/ap/config proxy routes and the
Network pane card.

The bridge owns the actual files (see test_host_bridge.py); these cover the
app's validators, the proxy's error degradation off a Pi and with no bridge,
and that the settings card renders on Pi shapes only.

Run: python -m pytest tests/test_ap_hotspot.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

from app.config import settings  # noqa: E402


class _Resp:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data or {}

    def json(self):
        return self._data


class _FakeBridge:
    """Async-context httpx stand-in dispatching bridge calls by URL suffix."""

    def __init__(self, routes, calls=None):
        self._routes = routes
        self.calls = calls if calls is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _match(self, url):
        for suffix, resp in self._routes.items():
            if url.endswith(suffix):
                return resp
        return _Resp(404)

    async def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self._match(url)

    async def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self._match(url)


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app

    cwd = os.getcwd()
    os.chdir(_SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_password", "")
    # Grocy configured so the setup-redirect middleware stays out of the way.
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test")
    monkeypatch.setattr(settings, "grocy_api_key", "k")
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


# --- pure validators ---------------------------------------------------------

def test_validate_ap_ssid_truth_table():
    from app.routers.setup import validate_ap_ssid

    ok = ["FoodAssistant", "Kitchen Pi", "a", "x" * 32]
    bad = ["", "   ", " edge", "edge ", "x" * 33, "two\nlines", "tab\tname",
           "café" + "x" * 29]
    for ssid in ok:
        assert validate_ap_ssid(ssid) == "", ssid
    for ssid in bad:
        assert validate_ap_ssid(ssid) != "", ssid


def test_validate_ap_passphrase_truth_table():
    from app.routers.setup import validate_ap_passphrase

    ok = ["goodpass", "x" * 63, "with spaces ok", "punct!@#$%"]
    bad = ["", "x" * 7, "x" * 64, "café-password", "line\nbreak-pw"]
    for pw in ok:
        assert validate_ap_passphrase(pw) == "", pw
    for pw in bad:
        assert validate_ap_passphrase(pw) != "", pw


# --- proxy routes ------------------------------------------------------------

def test_ap_config_get_off_pi_degrades_cleanly(client):
    with patch("app.routers.setup.is_raspberry_pi", return_value=False):
        r = client.get("/setup/ap/config")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is False and "platform" in d["error"]


def test_ap_config_get_proxies_the_bridge(client):
    routes = {"/ap/config": _Resp(200, {
        "ok": True, "active": False, "ssid": "Kitchen Pi",
        "default_ssid": "FoodAssistant", "generated": True,
        "passphrase": "abcd-efgh-jkmn",
    })}
    with patch("app.routers.setup.is_raspberry_pi", return_value=True), \
         patch("app.routers.setup.bridge_client",
               side_effect=lambda *a, **k: _FakeBridge(routes)):
        r = client.get("/setup/ap/config")
    d = r.json()
    assert d["ok"] is True and d["ssid"] == "Kitchen Pi"
    assert d["passphrase"] == "abcd-efgh-jkmn"


def test_ap_config_get_bridge_down_reports_error(client):
    class _Boom:
        async def __aenter__(self):
            raise RuntimeError("bridge down")

        async def __aexit__(self, *a):
            return False

    with patch("app.routers.setup.is_raspberry_pi", return_value=True), \
         patch("app.routers.setup.bridge_client",
               side_effect=lambda *a, **k: _Boom()):
        r = client.get("/setup/ap/config")
    d = r.json()
    assert d["ok"] is False and "bridge down" in d["error"]


def test_ap_config_post_validates_before_proxying(client):
    calls = []
    routes = {"/ap/config": _Resp(200, {"ok": True})}
    with patch("app.routers.setup.is_raspberry_pi", return_value=True), \
         patch("app.routers.setup.bridge_client",
               side_effect=lambda *a, **k: _FakeBridge(routes, calls)):
        r1 = client.post("/setup/ap/config", json={"ssid": "x" * 33})
        r2 = client.post("/setup/ap/config", json={"passphrase": "short"})
        r3 = client.post("/setup/ap/config", json={})
    assert r1.status_code == 400 and "32 bytes" in r1.json()["error"]
    assert r2.status_code == 400 and "8 to 63" in r2.json()["error"]
    assert r3.status_code == 400  # nothing to change
    assert calls == [], "invalid payloads must never reach the bridge"


def test_ap_config_post_forwards_valid_payload(client):
    calls = []
    routes = {"/ap/config": _Resp(200, {
        "ok": True, "active": False, "ssid": "Kitchen Pi", "generated": False,
    })}
    with patch("app.routers.setup.is_raspberry_pi", return_value=True), \
         patch("app.routers.setup.bridge_client",
               side_effect=lambda *a, **k: _FakeBridge(routes, calls)):
        r = client.post("/setup/ap/config",
                        json={"ssid": "Kitchen Pi", "passphrase": "new-pass-99"})
    assert r.json()["ok"] is True
    assert len(calls) == 1
    method, url, kw = calls[0]
    assert method == "POST" and url.endswith("/ap/config")
    assert kw["json"] == {"ssid": "Kitchen Pi", "passphrase": "new-pass-99"}


def test_ap_config_post_empty_ssid_resets_and_regenerate_wins(client):
    calls = []
    routes = {"/ap/config": _Resp(200, {"ok": True})}
    with patch("app.routers.setup.is_raspberry_pi", return_value=True), \
         patch("app.routers.setup.bridge_client",
               side_effect=lambda *a, **k: _FakeBridge(routes, calls)):
        client.post("/setup/ap/config", json={"ssid": ""})
        client.post("/setup/ap/config",
                    json={"regenerate": True, "passphrase": "ignored-pw"})
    # An empty ssid is forwarded as the reset-to-default signal.
    assert calls[0][2]["json"] == {"ssid": ""}
    # regenerate wins over a simultaneous passphrase.
    assert calls[1][2]["json"] == {"regenerate": True}


def test_ap_config_post_off_pi_degrades_cleanly(client):
    with patch("app.routers.setup.is_raspberry_pi", return_value=False):
        r = client.post("/setup/ap/config", json={"ssid": "Anything"})
    d = r.json()
    assert d["ok"] is False and "platform" in d["error"]


# --- pane render -------------------------------------------------------------

def _render_setup(client, is_pi):
    with patch.object(type(settings), "is_configured", lambda self: True), \
         patch("app.services.readiness.gate_possible", return_value=False), \
         patch("app.routers.setup.is_raspberry_pi", return_value=is_pi), \
         patch("app.templating.is_raspberry_pi", return_value=is_pi):
        r = client.get("/setup")
    assert r.status_code == 200
    return r.text


def test_hotspot_card_renders_on_pi_only(client, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted")
    html = _render_setup(client, is_pi=True)
    assert "Recovery hotspot" in html
    assert 'id="ap_ssid"' in html
    assert 'id="ap_passphrase"' in html
    assert 'id="ap-config-status"' in html
    # Honest copy: saving while joined through the hotspot drops the session.
    assert "saving drops your connection" in html

    monkeypatch.setattr(settings, "deployment_mode", "server")
    html = _render_setup(client, is_pi=False)
    assert 'id="ap_ssid"' not in html
    assert 'id="ap-config-status"' not in html


def test_hotspot_card_renders_on_pi_remote_too(client, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    html = _render_setup(client, is_pi=True)
    assert 'id="ap_ssid"' in html
    assert 'id="ap_passphrase"' in html
