"""Smaller 0.21 changes on the Settings page and its helper routes.

- The pairing list names each kind of device exactly as Settings, Security
  does (access.SCOPE_LABELS), so the two never drift.
- The Updates pane: the copy for a server that updates through its host
  helper, and the Docker-gateway line on a Compose or container install.
- The pairing relay tells the server this device is a screen.
- The hotspot settings pass the SD card copy choice both ways.
- The Settings page carries the access banner and the Home Assistant account
  line and token-holder list.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.routers import setup as setup_router  # noqa: E402
from app.services import access, update_path  # noqa: E402

HARDWARE_JS = SERVICE / "app" / "static" / "js" / "setup" / "hardware.js"


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    with patch.object(type(settings), "is_configured", lambda self: True):
        yield TestClient(app, client=("192.168.1.20", 50000))


def _resolved(monkeypatch, kind, bridge_state="absent", hint=False):
    async def fake(force=False):
        return {"kind": kind, "bridge": bridge_state, "bridge_hint": hint}
    monkeypatch.setattr(update_path, "resolve", fake)


def _banner(monkeypatch, **over):
    base = {"open_notice": False, "grace_until": None, "grace_address": "",
            "grace_kind": None, "gateway_peer": False}
    base.update(over)
    monkeypatch.setattr(access, "banner_context", lambda request: dict(base))


# -- pairing kinds ------------------------------------------------------------------

def test_pairing_kind_labels_match_the_security_pane():
    src = HARDWARE_JS.read_text()
    block = src[src.index("const _PAIRING_KINDS"):src.index("];", src.index("const _PAIRING_KINDS"))]
    pairs = dict(re.findall(r"\['([a-z_]+)', '([^']+)'\]", block))
    for kind in ("satellite", "ha", "ha_rest", "display"):
        assert pairs[kind] == access.SCOPE_LABELS[kind], kind
    assert set(pairs) == {"satellite", "ha", "ha_rest", "display"}


def test_pairing_relay_says_this_is_a_screen(client, monkeypatch):
    sent = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"ok": True, "request_id": "r1", "code": "1234", "expires_in": 300}

    class _C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, **k):
            sent["url"], sent["json"] = url, json
            return _Resp()
    monkeypatch.setattr(setup_router.httpx, "AsyncClient", _C)
    body = client.post("/setup/pairing/request",
                       json={"server_url": "http://192.0.2.5:9284"}).json()
    assert body["ok"] is True
    assert sent["json"]["kind"] == "satellite"
    assert isinstance(sent["json"]["hostname"], str)


# -- the Updates pane ---------------------------------------------------------------

def test_server_bridge_hint_is_user_forward(client, monkeypatch):
    _resolved(monkeypatch, "server_bridge", "verified")
    html = client.get("/setup").text
    assert ("Installs the newest tested release. Your inventory and recipes stay available "
            "while it updates, and if anything goes wrong you keep the version you have now."
            ) in html
    assert "Downloads the latest and restarts the app." not in html


@pytest.mark.parametrize("kind", ["watchtower", "container"])
def test_gateway_hint_on_compose_and_container_kinds(client, monkeypatch, kind):
    if kind == "watchtower":
        monkeypatch.setenv("WATCHTOWER_HTTP_API_TOKEN", "secret")
    _resolved(monkeypatch, kind)
    _banner(monkeypatch, gateway_peer=True)
    html = client.get("/setup").text
    m = re.search(r'<div class="small text-secondary mt-2" id="update-gateway-hint">(.*?)</div>',
                  html, re.S)
    assert m, kind
    import html as _html
    assert _html.unescape(m.group(1)) == access.GATEWAY_HINT


def test_no_gateway_hint_without_a_gateway_peer(client, monkeypatch):
    _resolved(monkeypatch, "container")
    _banner(monkeypatch, gateway_peer=False)
    assert 'id="update-gateway-hint"' not in client.get("/setup").text


def test_no_gateway_hint_for_a_server_bridge(client, monkeypatch):
    _resolved(monkeypatch, "server_bridge", "verified")
    _banner(monkeypatch, gateway_peer=True)
    assert 'id="update-gateway-hint"' not in client.get("/setup").text


# -- hotspot boot copy --------------------------------------------------------------

class _Bridge:
    def __init__(self, get_body):
        self.get_body = get_body
        self.posted = []

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **k):
        return _R(self.get_body)

    async def post(self, url, json=None, **k):
        self.posted.append(json)
        return _R({"ok": True, "ssid": "FoodAssistant", "boot_copy": json.get("boot_copy")})


class _R:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def test_ap_config_passes_boot_copy_both_ways(client, monkeypatch):
    b = _Bridge({"ok": True, "ssid": "FoodAssistant", "active": False, "boot_copy": True})
    monkeypatch.setattr(setup_router, "bridge_client", b)
    monkeypatch.setattr(setup_router, "is_raspberry_pi", lambda: True)
    assert client.get("/setup/ap/config").json()["boot_copy"] is True
    r = client.post("/setup/ap/config", json={"boot_copy": False})
    assert r.json()["ok"] is True
    assert b.posted == [{"boot_copy": False}]
    r = client.post("/setup/ap/config", json={})
    assert r.status_code == 400 and r.json()["error"] == "Nothing to change."
    assert len(b.posted) == 1


def test_network_pane_has_the_boot_copy_switch(client, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted", raising=False)
    with patch("app.services.readiness.gate_possible", return_value=False), \
         patch("app.routers.setup.is_raspberry_pi", return_value=True), \
         patch("app.templating.is_raspberry_pi", return_value=True):
        html = client.get("/setup").text
    assert 'id="ap_boot_copy"' in html
    assert "Anyone who can read the card can join the hotspot." in html


# -- banner and Home Assistant context -------------------------------------------------

def test_settings_page_renders_the_access_banner(client, monkeypatch):
    _banner(monkeypatch, open_notice=True)
    assert 'id="pr-access-banner"' in client.get("/setup").text
    _banner(monkeypatch)
    assert 'id="pr-access-banner"' not in client.get("/setup").text


def test_settings_page_renders_the_home_assistant_account_and_holders(client, monkeypatch):
    from app.services import devices, ha_client
    monkeypatch.setattr(settings, "streamdeck_ha_token", "server-ha-token", raising=False)
    monkeypatch.setattr(ha_client, "status_snapshot", lambda now=None: {
        "kind": "dedicated", "admin": False, "expires": "", "expires_soon": False,
        "probe_alert": None, "checked_at": None})
    monkeypatch.setattr(devices, "list_devices", lambda: [
        {"deployment_mode": "pi_remote", "source": "heartbeat", "label": "Pantry screen",
         "relay": True, "deck_proxy": False},
        {"deployment_mode": "pi_remote", "source": "heartbeat", "hostname": "garage-pi",
         "relay": False},
        {"deployment_mode": "pi_remote", "source": "heartbeat", "label": "Updated one",
         "relay": True, "deck_proxy": True},
        {"deployment_mode": "server", "source": "scan", "label": "Other server"},
    ])
    html = client.get("/setup").text
    assert 'id="ha-account-kind"' in html
    assert "A limited Pantry Raider account in Home Assistant." in html
    m = re.search(r'id="ha-token-holders">(.*?)</div>', html, re.S)
    assert m and "Pantry screen" in m.group(1) and "garage-pi" in m.group(1)
    assert "Updated one" not in m.group(1) and "Other server" not in m.group(1)
    assert "server-ha-token" not in html


def test_no_token_holders_without_a_token_on_this_install(client, monkeypatch):
    from app.services import devices
    monkeypatch.setattr(settings, "streamdeck_ha_token", "", raising=False)
    monkeypatch.setattr(devices, "list_devices", lambda: [
        {"deployment_mode": "pi_remote", "source": "heartbeat", "hostname": "garage-pi",
         "relay": False}])
    assert setup_router._ha_token_holders() == []
    assert 'id="ha-token-holders"' not in client.get("/setup").text


def test_print_service_context_is_passed(client, monkeypatch):
    from app.services import printing
    monkeypatch.setattr(printing, "print_service_state", lambda: "legacy")
    monkeypatch.setattr(printing, "print_service_fix_kind", lambda: "compose")
    captured = {}
    real = setup_router.templates.TemplateResponse

    def spy(request, name, context, *a, **k):
        captured.update(context)
        return real(request, name, context, *a, **k)
    monkeypatch.setattr(setup_router.templates, "TemplateResponse", spy)
    client.get("/setup")
    assert captured["print_service"] == "legacy"
    assert captured["print_service_fix"] == "compose"
