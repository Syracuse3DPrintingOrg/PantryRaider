"""Pi host page decision logic (FoodAssistant-mvke, FoodAssistant-1idf).

Covers the pure helpers that pick the Mealie card affordance and classify the
active network link, plus the route/template plumbing that hides the "Start
Mealie on this device" button when Mealie is already running and softens the
Wi-Fi line to a calm "not in use" when the Pi is on Ethernet.

Run: python -m pytest tests/test_host_panel.py -q
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
from app.services.host_panel import (  # noqa: E402
    mealie_action_state,
    classify_active_connection,
)


# --- mealie_action_state ----------------------------------------------------

def test_mealie_action_running_shows_connect():
    assert mealie_action_state(installed=True, running=True) == "running"


def test_mealie_action_installed_but_stopped_is_none():
    # We never offer to start/install Mealie from the UI (FoodAssistant-9mu5):
    # a stopped Mealie is "none", not a start button.
    assert mealie_action_state(installed=True, running=False) == "none"


def test_mealie_action_not_installed_is_none():
    # Not installed is also "none": no UI install path. Start it over SSH.
    assert mealie_action_state(installed=False, running=False) == "none"


def test_mealie_action_unavailable_is_none():
    # Off a Pi appliance, or a satellite pointing at a remote stack.
    assert mealie_action_state(installed=True, running=True, available=False) == "none"
    assert mealie_action_state(installed=False, running=False, available=False) == "none"


# --- classify_active_connection ---------------------------------------------

def test_classify_wired_eth0():
    out = "default via 192.168.1.1 dev eth0 proto dhcp metric 100\n"
    assert classify_active_connection(out) == "wired"


def test_classify_wired_end0_pi5():
    out = "default via 192.168.1.1 dev end0 proto dhcp src 192.168.1.50 metric 100\n"
    assert classify_active_connection(out) == "wired"


def test_classify_wired_enx_predictable():
    out = "default via 10.0.0.1 dev enxb827eb000000 proto dhcp metric 100\n"
    assert classify_active_connection(out) == "wired"


def test_classify_wifi_wlan0():
    out = "default via 192.168.1.1 dev wlan0 proto dhcp metric 600\n"
    assert classify_active_connection(out) == "wifi"


def test_classify_prefers_lower_metric_wired_over_wifi():
    # Both links up: the wired route has the lower metric and should win.
    out = (
        "default via 192.168.1.1 dev wlan0 proto dhcp metric 600\n"
        "default via 192.168.1.1 dev end0 proto dhcp metric 100\n"
    )
    assert classify_active_connection(out) == "wired"


def test_classify_none_when_no_default_route():
    # The fallback hotspot assigns a static wlan0 address but no default route.
    assert classify_active_connection("") == "none"
    assert classify_active_connection("192.168.4.1 dev wlan0 scope link\n") == "none"


def test_classify_ignores_virtual_only_routes():
    out = "default dev tun0 scope link metric 50\n"
    assert classify_active_connection(out) == "none"


# --- route + template plumbing ----------------------------------------------

class _Resp:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data or {}

    def json(self):
        return self._data


class _FakeBridge:
    """Async-context httpx stand-in dispatching bridge GETs by URL suffix."""

    def __init__(self, routes):
        self._routes = routes

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
        return self._match(url)

    async def post(self, url, **kw):
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
    # Grocy configured so the setup GET does not probe for a local Grocy.
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test")
    monkeypatch.setattr(settings, "grocy_api_key", "k")
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted")
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _render_setup(client, routes):
    def factory(*a, **kw):
        return _FakeBridge(routes)

    with patch("app.routers.setup.is_raspberry_pi", return_value=True), \
         patch("app.templating.is_raspberry_pi", return_value=True), \
         patch("app.routers.setup.bridge_client", side_effect=factory), \
         patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/setup")
    assert r.status_code == 200
    return r.text


def test_setup_hides_start_button_when_mealie_running(client):
    html = _render_setup(client, {
        "/mealie/status": _Resp(200, {"ok": True, "state": "running", "running": True}),
    })
    assert "Start Mealie on this device" not in html
    assert "Mealie is running on this device" in html


def test_setup_never_offers_to_start_mealie_when_not_installed(client):
    # Mealie is never installed/started from the UI (FoodAssistant-9mu5): a
    # not-installed Mealie shows no start button. Someone who wants it starts it
    # over SSH; the connect (URL + token) fields still appear.
    html = _render_setup(client, {
        "/mealie/status": _Resp(200, {"ok": True, "state": "not-installed", "running": False}),
    })
    assert "Start Mealie on this device" not in html
    assert "Mealie is running on this device" not in html


def test_setup_never_offers_to_start_mealie_when_bridge_unreachable(client):
    # A bridge hiccup must not crash the settings page, and still must not offer
    # a UI install/start of Mealie.
    class _Boom:
        async def __aenter__(self):
            raise RuntimeError("bridge down")

        async def __aexit__(self, *a):
            return False

    with patch("app.routers.setup.is_raspberry_pi", return_value=True), \
         patch("app.templating.is_raspberry_pi", return_value=True), \
         patch("app.routers.setup.bridge_client", side_effect=lambda *a, **k: _Boom()), \
         patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/setup")
    assert r.status_code == 200
    assert "Start Mealie on this device" not in r.text


def test_network_status_reports_wired_active_connection(client):
    routes = {
        "/wifi/status": _Resp(200, {
            "ok": True, "state": "disconnected", "ssid": "",
            "ethernet": {"connected": True, "device": "end0", "ip": "192.168.1.50"},
            "default_route": "default via 192.168.1.1 dev end0 proto dhcp metric 100\n",
        }),
        "/hostname": _Resp(200, {"hostname": "kitchen-pi"}),
    }

    def factory(*a, **kw):
        return _FakeBridge(routes)

    with patch("app.routers.setup.is_raspberry_pi", return_value=True), \
         patch("app.routers.setup.bridge_client", side_effect=factory):
        r = client.get("/setup/network/status")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["active_connection"] == "wired"
    assert d["ethernet"]["connected"] is True


def test_network_status_reports_wifi_active_connection(client):
    routes = {
        "/wifi/status": _Resp(200, {
            "ok": True, "state": "connected", "ssid": "HomeNet",
            "ethernet": {"connected": False},
            "default_route": "default via 192.168.1.1 dev wlan0 proto dhcp metric 600\n",
        }),
        "/hostname": _Resp(200, {"hostname": "kitchen-pi"}),
    }

    def factory(*a, **kw):
        return _FakeBridge(routes)

    with patch("app.routers.setup.is_raspberry_pi", return_value=True), \
         patch("app.routers.setup.bridge_client", side_effect=factory):
        r = client.get("/setup/network/status")
    assert r.status_code == 200
    assert r.json()["active_connection"] == "wifi"
