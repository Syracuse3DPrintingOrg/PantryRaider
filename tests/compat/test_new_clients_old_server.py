"""This release's clients against a server still on v0.20.0.

A satellite, the Home Assistant integration, the Stream Deck, the kiosk
launcher and the sensor reader from this release can meet a main server that
has not updated yet. responses-v0.20.0.json holds what the real v0.20.0 app
answered to each of their requests (captured at build time by
scripts/compat/capture-client-fixtures.py); every test here hands that
recorded answer to the client's own contract function and checks the client
keeps working the old way.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
import threading
import types
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPONENT = ROOT / "homeassistant" / "custom_components" / "pantry_raider"
LAUNCHER = ROOT / "scripts" / "image-build" / "foodassistant-kiosk-launch"


# --- Satellite (Z03) ---------------------------------------------------------------


def test_satellite_applies_an_old_servers_pull_without_relaying(compat, monkeypatch,
                                                                tmp_path):
    """The recorded v0.20.0 pull is signed and carries no fleet_relay block:
    this release's satellite applies it, keeps fleet_relay {}, keeps the
    credentials it carries and scrubs nothing."""
    from app.config import settings
    from app.services import satellite

    pull = compat.RESPONSES["satellite_pull"]
    assert pull["status"] == 200 and "fleet_relay" not in pull["json"]["config"]
    compat.configure_satellite(monkeypatch, tmp_path, upstream_api_key=pull["key"],
                               fleet_relay={}, gemini_api_key="",
                               streamdeck_ha_token="")
    sent = {}

    def fake_get(url, headers=None, timeout=None):
        sent.update(url=url, headers=dict(headers or {}))
        return httpx.Response(pull["status"], json=pull["json"],
                              request=httpx.Request("GET", url))

    scrubbed = []
    monkeypatch.setattr(satellite.httpx, "get", fake_get)
    monkeypatch.setattr(satellite._secrets, "token_hex", lambda n=16: pull["nonce"])
    monkeypatch.setattr(satellite, "scrub_relayed_credentials",
                        lambda cfg: scrubbed.append(cfg) or [])
    for name in ("_apply_defaults", "_apply_profiles"):
        monkeypatch.setattr(satellite, name, lambda rows: 0)
    monkeypatch.setattr(satellite, "_apply_gadget_config", lambda data: None)
    monkeypatch.setattr(satellite, "mirror_alarms", lambda alarms: 0)
    monkeypatch.setattr(satellite, "_push_streamdeck_settings", lambda *a, **k: False)
    monkeypatch.setattr(satellite, "_resolve_host", lambda host: "")

    result = satellite._do_sync_from_upstream()

    assert result["ok"] is True, result
    assert sent["url"].endswith("/api/config/satellite")
    assert sent["headers"]["X-API-Key"] == pull["key"]
    assert settings.fleet_relay == {}
    assert int(settings.upstream_ha_policy_version or 0) == 0
    assert scrubbed == []
    cfg = pull["json"]["config"]
    assert settings.gemini_api_key == cfg["gemini_api_key"]
    assert settings.streamdeck_ha_token == cfg["streamdeck_ha_token"]


# --- Home Assistant integration (Z09) ----------------------------------------------


def _load_component(monkeypatch):
    """The integration's api.py and helpers.py, without Home Assistant: a
    package object pointing at the component directory (its __init__.py,
    which needs Home Assistant, is never run) and small aiohttp stand-ins."""
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientError = type("ClientError", (Exception,), {})
    aiohttp.ClientSession = object
    async_timeout = types.ModuleType("async_timeout")

    class _Timeout:
        def __init__(self, *_a, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False
    async_timeout.timeout = _Timeout
    monkeypatch.setitem(sys.modules, "aiohttp", aiohttp)
    monkeypatch.setitem(sys.modules, "async_timeout", async_timeout)
    pkg = types.ModuleType("pr_compat_pantry_raider")
    pkg.__path__ = [str(COMPONENT)]
    monkeypatch.setitem(sys.modules, "pr_compat_pantry_raider", pkg)
    for name in ("pr_compat_pantry_raider.api", "pr_compat_pantry_raider.helpers"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    api = importlib.import_module("pr_compat_pantry_raider.api")
    helpers = importlib.import_module("pr_compat_pantry_raider.helpers")
    return api, helpers


class _Recorded:
    """An aiohttp-shaped session that answers every call with one recorded
    v0.20.0 response."""

    def __init__(self, recorded):
        self.recorded = recorded
        self.calls = []

    def _answer(self, method, url, headers):
        self.calls.append((method, url, dict(headers or {})))
        recorded = self.recorded

        class _Resp:
            status = recorded["status"]

            async def json(self):
                return recorded["json"]
        return _Resp()

    async def get(self, url, headers=None, **_kw):
        return self._answer("GET", url, headers)

    async def post(self, url, headers=None, **_kw):
        return self._answer("POST", url, headers)


def test_integration_accepts_a_pairing_status_with_the_full_key(compat, monkeypatch):
    _api, helpers = _load_component(monkeypatch)
    status = compat.RESPONSES["pairing_status_approved"]
    assert status["status"] == 200 and status["json"]["api_key"]
    assert helpers.pairing_result(status["json"]) == "approved"


def test_integration_keeps_its_key_when_the_exchange_is_not_there(compat, monkeypatch):
    """v0.20.0 answers the key exchange with 404: the client says the install
    is older, and the key it holds is unchanged."""
    api, _helpers = _load_component(monkeypatch)
    recorded = compat.RESPONSES["credentials_exchange"]
    assert recorded["status"] == 404
    session = _Recorded(recorded)
    client = api.PantryRaiderClient(session, "http://192.168.1.50:9284", "old-shared-key",
                                    client_version="0.21.0")
    with pytest.raises(api.PantryRaiderNotSupported):
        asyncio.run(client.async_exchange_legacy_key())
    assert issubclass(api.PantryRaiderNotSupported, api.PantryRaiderConnectionError)
    assert client.api_key == "old-shared-key"
    method, url, headers = session.calls[0]
    assert (method, url) == ("POST", "http://192.168.1.50:9284/api/credentials/exchange")
    assert headers["X-API-Key"] == "old-shared-key"


def test_integration_keeps_the_owner_token_path_for_an_old_install(compat, monkeypatch):
    """/ha/state from v0.20.0 has no features list: the integration hands the
    install an owner token the old way and raises the update Repairs issue."""
    _api, helpers = _load_component(monkeypatch)
    state = compat.RESPONSES["ha_state"]
    assert state["status"] == 200 and "features" not in state["json"]
    assert helpers.dedicated_supported(state["json"]) is False
    plan = helpers.handover_plan(
        is_server=helpers.is_server_mode(state["json"].get("mode")), connect_back=True,
        state=state["json"], options={},
        now_iso=datetime.now(timezone.utc).isoformat(), used_internal_url=True)
    assert plan["action"] == "owner"
    assert plan["mint"] is True
    assert plan["repair"] == helpers.ISSUE_APP_UPDATE
    # The old install never names a countdown, so no grace issue shows.
    assert helpers.grace_issue_id(state["json"]) is None


# --- Stream Deck (Z18) ----------------------------------------------------------------


def test_deck_runs_an_elevated_key_under_an_old_config(compat, tmp_path, monkeypatch):
    """The config.toml the v0.20.0 bridge writes has no ha_policy_version, so
    the deck treats it as version 0 and an elevated key (a lock) still runs."""
    from foodassistant_streamdeck import actions, config as deck_config

    recorded = compat.RESPONSES["streamdeck_config_toml"]["text"]
    assert "ha_policy_version" not in recorded
    path = tmp_path / "config.toml"
    path.write_text(recorded)
    cfg = deck_config.load(path)
    assert cfg.ha_policy_version == 0
    lock = next(o for o in cfg.key_overrides if o.get("service") == "lock.unlock")
    spec = actions.override_to_spec(int(lock["slot"]), lock, cfg.ha_policy_version)
    assert spec is not None and not spec.ha_needs_ok

    posted = []

    class _HA:
        def __init__(self, *_a, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, url, json=None, headers=None):
            posted.append((url, json, headers))
            return httpx.Response(200, json=[])

    monkeypatch.setattr(httpx, "AsyncClient", _HA)

    async def noop(*_a, **_kw):
        return True
    ctx = actions.ActionContext(
        client=None, base_url=cfg.base_url, refresh=noop, navigate=noop,
        cycle_brightness=lambda: 60, page_next=lambda: None, page_prev=lambda: None,
        ha_base_url=cfg.ha_base_url, ha_token=cfg.ha_token,
        ha_policy_version=cfg.ha_policy_version)
    face = asyncio.run(actions.run_action(spec, ctx))
    assert posted, face
    url, body, headers = posted[0]
    assert url == cfg.ha_base_url + "/api/services/lock/unlock"
    assert body == {"entity_id": "lock.front_door"}
    assert headers["Authorization"] == "Bearer " + cfg.ha_token


# --- Kiosk launcher (Z17) -------------------------------------------------------------


def _load_launcher():
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("pr_compat_kiosk_launch", str(LAUNCHER))
    spec = importlib.util.spec_from_loader("pr_compat_kiosk_launch", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_launcher_keeps_the_plain_url_when_the_probe_is_not_there(compat, tmp_path,
                                                                 monkeypatch):
    """v0.20.0 answers GET /ui/kiosk/launch?probe=1 with 404: the launcher
    opens the kiosk URL as it is and keeps the ticket out of it."""
    recorded = compat.RESPONSES["kiosk_launch_probe"]
    assert recorded["status"] == 404
    body = json.dumps(recorded["json"]).encode()
    paths = []

    class _Old(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            paths.append(self.path)
            self.send_response(recorded["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Old)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        launcher = _load_launcher()
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        ticket = compat.VECTORS["tickets"][0]["ticket"]
        (run_dir / "ticket").write_text(ticket)
        monkeypatch.setenv("KIOSK_RUN_DIR", str(run_dir))
        monkeypatch.setenv("FA_PROBE_RETRY_SECONDS", "0.05")
        url = f"http://127.0.0.1:{server.server_address[1]}/ui/?kiosk=1"
        args = ["/usr/bin/chromium", "--kiosk", url]
        out, page = launcher.ticket_step(list(args), hostname=compat.OWN_NAME)
    finally:
        server.shutdown()
        server.server_close()
    assert out == args and page == ""
    assert paths == ["/ui/kiosk/launch?probe=1"]
    assert ticket not in " ".join(out)


# --- Sensor reader (Z18) -------------------------------------------------------------


def test_reader_sends_its_key_and_name_to_an_old_server(compat, monkeypatch):
    """The reader names itself (X-PR-Client) and keeps sending its key
    (X-API-Key, the header v0.20.0 checks), and reads v0.20.0's
    /gadgets/config answer."""
    bleak = types.ModuleType("bleak")
    bleak.BleakClient = bleak.BleakScanner = object
    monkeypatch.setitem(sys.modules, "bleak", bleak)
    monkeypatch.delitem(sys.modules, "foodassistant_gadgets.daemon", raising=False)
    from foodassistant_gadgets import __version__
    from foodassistant_gadgets.config import Config
    from foodassistant_gadgets.daemon import Daemon

    recorded = compat.RESPONSES["gadgets_config"]
    assert recorded["status"] == 200
    seen = []

    def answer(request):
        seen.append(request)
        return httpx.Response(recorded["status"], json=recorded["json"])

    async def run():
        daemon = Daemon(Config(base_url="http://127.0.0.1:9284", api_key="reader-key"))
        await daemon._client.aclose()
        daemon._client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
        try:
            await daemon._pull_config()
        finally:
            await daemon._client.aclose()
        return daemon

    daemon = asyncio.run(run())
    assert seen and seen[0].url.path == "/gadgets/config"
    assert seen[0].headers["X-API-Key"] == "reader-key"
    assert seen[0].headers["X-PR-Client"] == f"gadgets/{__version__}"
    assert daemon.enabled is bool(recorded["json"].get("enabled"))
