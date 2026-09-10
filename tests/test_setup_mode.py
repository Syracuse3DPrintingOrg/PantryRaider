"""Tests for deployment-mode selection in the setup wizard.

Covers the mode taxonomy (Server hosted / Pi Hosted / Pi Remote), host-aware
filtering of the offered modes, persistence, and the relaxed is_configured()
rule for the thin-client Pi Remote mode.

The wizard reads Pi-ness through app.routers.setup.is_raspberry_pi /
board_model; we patch those bound names directly so the tests run on any host
without touching /proc/device-tree or fighting lru_cache.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
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


# -- Inventory step on a plain server (FoodAssistant-nkxd) -------------------
#
# The wizard's Inventory step polls this while it waits. It used to refuse off a
# Pi ("Not available on this platform"), so a first-time server user whose
# inventory container was still starting, or was never started, sat on a spinner
# with nothing to act on.

def test_local_inventory_probe_answers_on_a_plain_server(client, monkeypatch):
    _as_server(monkeypatch)

    async def _found():
        return "http://localhost:9383"
    monkeypatch.setattr(setup_router, "_detect_local_grocy", _found)

    body = client.get("/setup/grocy/local-status").json()
    assert body["ok"] is True
    assert body["serving"] is True
    # The wizard connects to the address that answered, so it is reported back.
    assert body["url"] == "http://localhost:9383"


def test_local_inventory_probe_reports_nothing_serving(client, monkeypatch):
    _as_server(monkeypatch)

    async def _none():
        return ""
    monkeypatch.setattr(setup_router, "_detect_local_grocy", _none)

    body = client.get("/setup/grocy/local-status").json()
    # ok stays True: the probe ran and the honest answer is "nothing is there".
    # An ok=False here reads as "cannot tell" and the wizard keeps waiting.
    assert body["ok"] is True
    assert body["serving"] is False
    assert body["url"] == ""


def test_local_inventory_probe_still_answers_on_an_appliance(client, monkeypatch):
    _as_pi(monkeypatch)

    async def _found():
        return "http://127.0.0.1:9383"
    monkeypatch.setattr(setup_router, "_detect_local_grocy", _found)

    body = client.get("/setup/grocy/local-status").json()
    assert body == {"ok": True, "serving": True, "url": "http://127.0.0.1:9383"}


def test_inventory_step_never_claims_there_is_nothing_to_configure(client, monkeypatch):
    _as_server(monkeypatch)
    html = client.get("/setup").text
    assert "nothing to configure here" not in html
    assert "nothing to press here" not in html
    # The wizard opens this disclosure and hides that hint when nothing answers,
    # so the address field is in front of the user instead of behind a summary.
    assert 'id="wiz-grocy-advanced"' in html
    assert 'id="wiz-grocy-auto-hint"' in html


def test_wizard_names_the_command_that_starts_the_built_in_inventory():
    js = (SERVICE / "app" / "static" / "js" / "setup" / "wizard.js").read_text()
    # The profile flag is the part people miss: a plain `docker compose up -d`
    # starts Pantry Raider with no inventory service behind it.
    assert "docker compose --profile with-grocy up -d" in js
    # And the app never runs it: no docker socket, and granting one would hand
    # the app full control of the host.
    assert "setup/docker" not in js


# --- The Inventory step's watch loop, run for real in node -------------------

_NODE = shutil.which("node")


def _inventory_watch_source() -> str:
    """The Inventory step's watch loop, sliced out of wizard.js.

    Sliced rather than copied so the test runs the shipped code. The anchors are
    the block's own first and last lines, so a move fails here loudly instead of
    quietly testing nothing.
    """
    js = (SERVICE / "app" / "static" / "js" / "setup" / "wizard.js").read_text()
    start = js.index("let _wizServerInvActive = false;")
    end = js.index("// While the appliance's Grocy is still being installed")
    return js[start:end]


_HARNESS = """
let _installMode = 'server';
let _wizStep = 1;
let rendered = '';
const el = {
  set innerHTML(v) { rendered = v; },
  get innerHTML() { return rendered; },
  classList: {add() {}, remove() {}},
  setAttribute() {},
};
global.window = {};
global.document = {getElementById: (id) => (id === 'grocy-install-result' ? el : null)};
function setResult(id, ok, msg) { rendered = msg; }
function val() { return ''; }
function _sleep() { return Promise.resolve(); }
function _wizBuildSummary() {}
function _fetchJson() { return Promise.resolve({ok: true, serving: true, url: 'http://x'}); }
function postJson() { return Promise.resolve(REPLY); }
%(source)s
_wizServerInventoryStatus().then(() => { console.log(JSON.stringify({rendered})); });
"""


def _run_watch_loop(tmp_path, reply: str) -> str:
    script = tmp_path / "watch.js"
    script.write_text("const REPLY = " + reply + ";\n"
                      + _HARNESS % {"source": _inventory_watch_source()})
    out = subprocess.run([_NODE, str(script)], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])["rendered"]


@pytest.mark.skipif(_NODE is None, reason="node is not available")
def test_a_refusal_from_the_server_is_shown_instead_of_the_docker_command(tmp_path):
    # Any reverse proxy (HA Ingress, Pangolin, SWAG, Traefik, Cloudflare) sets a
    # forwarding header, and the first-run endpoint refuses those with an
    # explanation. Waiting cannot change that answer, so the loop has to show it
    # rather than spend two minutes and then blame a missing container.
    from app.routers.setup import _FIRST_RUN_REMOTE_MSG
    rendered = _run_watch_loop(
        tmp_path, json.dumps({"ok": False, "error": _FIRST_RUN_REMOTE_MSG}))
    assert rendered == _FIRST_RUN_REMOTE_MSG
    assert "docker compose" not in rendered


@pytest.mark.skipif(_NODE is None, reason="node is not available")
def test_nothing_answering_at_all_still_ends_on_the_docker_command(tmp_path):
    # The other half of the same behaviour: a reply with no explanation means
    # the inventory service simply is not up, which is what the command is for.
    rendered = _run_watch_loop(
        tmp_path, json.dumps({"ok": False, "configured": False,
                              "message": "Could not reach it yet."}))
    assert "docker compose --profile with-grocy up -d" in rendered
