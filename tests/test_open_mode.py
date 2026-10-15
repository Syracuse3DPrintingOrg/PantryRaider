"""An install with no password (open mode, D2 step 5) and the satellite gate (D3)."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.config import settings
from app.services import access, auth_state, lan

SERVICE = Path(__file__).resolve().parents[1] / "service"
DAY = 86400.0


@pytest.fixture
def items(monkeypatch):
    seen = []
    monkeypatch.setattr(access, "raise_item",
                        lambda kind, title, body, dedupe, level="info":
                        seen.append({"kind": kind, "title": title, "body": body,
                                     "dedupe": dedupe, "level": level}))
    return seen


@pytest.fixture
def open_install(tmp_path, monkeypatch):
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "api_key", "", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", [], raising=False)
    monkeypatch.setattr(settings, "require_key_families", {}, raising=False)
    monkeypatch.setattr(settings, "home_host_names", "", raising=False)
    monkeypatch.setattr(settings, "demo_mode", False, raising=False)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True, raising=False)
    from app.services import readiness, timers
    monkeypatch.setattr(readiness, "gate_possible", lambda: False)
    yield tmp_path
    # Timers created through the routes here must not reach later tests.
    timers.clear_all()


def _client(peer=("192.168.1.50", 50000), base_url="http://testserver", headers=None):
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, client=peer, base_url=base_url, headers=headers or {})


PROXIED = {"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Host": "pantry.example.com"}
HTML = {"Accept": "text/html"}


# --- Home origins ---------------------------------------------------------------

@pytest.mark.parametrize("host", ["192.168.1.5:9284", "pr.local:9284", "kitchen:9284",
                                  "[fd00::5]:9284"])
def test_home_names_are_always_served(open_install, grace_over, host):
    assert _client().get("/timers", headers={"Host": host}).status_code == 200


def test_ingress_is_always_served(open_install, grace_over, ingress_client):
    assert ingress_client.get("/timers").status_code == 200
    # A device family through Ingress needs a key.
    assert ingress_client.get("/ha/state").status_code == 401


def test_a_foreign_origin_write_is_refused(open_install):
    c = _client()
    r = c.post("/timers", json={"label": "x", "seconds": 60},
               headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and r.json()["code"] == "cross_site"
    r = c.post("/timers", json={"label": "x", "seconds": 60}, headers={"Origin": "null"})
    assert r.status_code == 403
    r = c.post("/timers", json={"label": "x", "seconds": 60},
               headers={"Origin": "http://testserver"})
    assert r.status_code != 403


def test_an_unknown_key_counts_as_no_key(open_install):
    assert _client().get("/timers", headers={"X-API-Key": "not-a-key"}).status_code == 200


def test_an_older_key_on_settings_from_home_is_judged_as_no_key(open_install, monkeypatch):
    from app.services import update_path

    async def _container(force=False):
        return {"kind": update_path.KIND_CONTAINER, "bridge": update_path.BRIDGE_ABSENT,
                "bridge_hint": False}

    monkeypatch.setattr(update_path, "resolve", _container)
    monkeypatch.setattr(settings, "api_key", "older-key", raising=False)
    r = _client().post("/setup/update", json={}, headers={"X-API-Key": "older-key"})
    # The route itself answered (a container has no in-app updater).
    assert r.status_code == 200 and r.json()["no_updater"] is True
    r = _client().post("/setup/update", json={}, headers={"X-API-Key": "older-key", **PROXIED})
    assert r.status_code == 403 and r.json()["detail"] == access.ADMIN_KEY_MSG


# --- Proxied and public visitors --------------------------------------------------

def test_proxied_and_public_are_served_in_the_grace_with_one_item(open_install, items):
    c = _client(headers=PROXIED)
    assert c.get("/timers").status_code == 200
    assert c.get("/timers").status_code == 200
    public = _client(peer=("203.0.113.9", 50000))
    assert public.get("/timers").status_code == 200
    grace = [i for i in items if i["kind"] == "open_mode_grace"]
    assert len({i["dedupe"] for i in grace}) == 1
    assert "testserver" in grace[0]["body"] or "pantry" in grace[0]["body"]
    seen, _ = auth_state.open_sightings()
    assert seen


def test_after_the_grace_outside_visitors_are_refused(open_install, grace_over, items):
    c = _client(headers=PROXIED)
    r = c.get("/timers")
    assert r.status_code == 403
    assert r.json() == {"detail": access.OPEN_REFUSAL_MSG, "code": "open_mode_refused"}
    page = c.get("/ui/", headers=HTML)
    assert page.status_code == 403
    html = page.text
    assert 'action="ui/setup-code"' in html
    assert html.index('action="ui/setup-code"') < html.index("At home, open this kitchen at")
    assert _client(peer=("203.0.113.9", 50000)).get("/timers").status_code == 403


def test_fresh_installs_have_no_grace(open_install, fresh_install):
    assert _client(headers=PROXIED).get("/timers").status_code == 403


def test_a_restore_restarts_the_grace(open_install, grace_over):
    assert _client(headers=PROXIED).get("/timers").status_code == 403
    auth_state.note_restore()
    assert _client(headers=PROXIED).get("/timers").status_code == 200


def test_unconfigured_outside_gets_the_setup_code(open_install, monkeypatch, capsys):
    monkeypatch.setattr(type(settings), "is_configured", lambda self: False, raising=False)
    r = _client(headers=PROXIED).get("/timers")
    assert r.status_code == 403 and r.json()["code"] == "setup_code_required"
    assert "Pantry Raider setup code:" in capsys.readouterr().out
    assert _client().get("/setup", follow_redirects=False).status_code == 200


def test_demo_mode_serves_outside_visitors(open_install, grace_over, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True, raising=False)
    c = _client(headers=PROXIED)
    assert c.get("/timers").status_code == 200
    r = c.post("/timers", json={"label": "x", "seconds": 5})
    assert r.status_code == 403
    assert r.json()["error"] == "demo_read_only"


# --- Names ------------------------------------------------------------------------

def test_an_unknown_name_in_and_after_the_grace(open_install, items, monkeypatch):
    c = _client(base_url="http://pantry.fritz.box:9284")
    assert c.get("/timers").status_code == 200
    names = [i for i in items if i["kind"] == "open_mode_name"]
    assert len(names) == 1 and "Other names for this kitchen" in names[0]["body"]
    assert "pantry.fritz.box" in names[0]["body"]
    seen, _ = auth_state.open_sightings()
    assert seen[0]["kind"] == "name"
    auth_state.reset(auth_state.test_state(open_mode={"grace_until": auth_state.iso(time.time() - DAY)}),
                     persist=False)
    r = c.get("/timers")
    assert r.status_code == 403 and r.json()["code"] == "open_mode_unknown_name"
    page = c.get("/ui/", headers=HTML)
    assert page.status_code == 403 and access.NAME_ALLOW_HINT in page.text.replace("&#39;", "'")
    monkeypatch.setattr(settings, "home_host_names", "pantry.fritz.box", raising=False)
    assert c.get("/timers").status_code == 200


def test_the_recovery_hotspot_skips_the_name_check(open_install, fresh_install, monkeypatch):
    c = _client(peer=("192.168.99.5", 50000), base_url="http://captive.apple.com")
    monkeypatch.setattr(lan, "recovery_hotspot_up", lambda: True)
    assert c.get("/timers").status_code == 200
    monkeypatch.setattr(lan, "recovery_hotspot_up", lambda: False)
    assert c.get("/timers").status_code == 403


def test_a_flood_of_names_stays_bounded(open_install, monkeypatch, items):
    auth_state.reset(auth_state.test_state(), persist=True)
    import os
    real = os.replace
    writes = []

    def counting(src, dst, *a, **k):
        if str(dst).endswith(auth_state.FILENAME):
            writes.append(dst)
        return real(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", counting)
    c = _client(headers={"X-Forwarded-For": "203.0.113.9"})
    for i in range(400):
        c.get("/timers", headers={"Host": f"h{i}.example", "X-Forwarded-For": f"198.51.100.{i % 250}"})
    seen, other = auth_state.open_sightings()
    assert len(seen) == 20 and other > 0
    assert len(writes) <= 2
    assert len([i for i in items if i["kind"] == "open_mode_grace"]) == 1


def _request(host, peer="192.168.1.50", headers=()):
    from starlette.requests import Request
    raw = [(b"host", host.encode())] + [(k.lower().encode(), v.encode()) for k, v in headers]
    return Request({"type": "http", "method": "GET", "path": "/timers", "headers": raw,
                    "client": (peer, 50000), "query_string": b"", "http_version": "1.1",
                    "scheme": "http", "server": ("testserver", 80), "root_path": ""})


def test_banner_context(open_install):
    assert _client(headers=PROXIED).get("/timers").status_code == 200
    body = access.banner_context(_request("pantry.example.com",
                                          headers=[("X-Forwarded-For", "203.0.113.9")]))
    assert body["grace_address"] == "pantry.example.com" and body["grace_kind"] == "proxied"
    assert body["open_notice"] is True and body["grace_until"]
    home = access.banner_context(_request("192.168.1.5:9284"))
    assert home["grace_address"] == "" and home["grace_kind"] == "proxied"
    assert home["grace_until"] and home["open_notice"] is True
    ingress = access.banner_context(_request("x", peer="172.30.32.2"))
    assert isinstance(ingress, dict)


def test_no_banner_in_demo_mode_or_with_a_password(open_install, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True, raising=False)
    assert access.banner_context(_request("192.168.1.5"))["open_notice"] is False
    monkeypatch.setattr(settings, "demo_mode", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "hashed", raising=False)
    body = access.banner_context(_request("192.168.1.5"))
    assert body["open_notice"] is False and body["grace_until"] is None


# --- Device families ------------------------------------------------------------

def test_family_defaults_on_an_upgraded_install(open_install):
    c = _client()
    assert c.get("/ha/state").status_code != 401
    assert c.get("/cub/summary").status_code != 401


def test_family_defaults_on_a_fresh_install(open_install, fresh_install):
    c = _client()
    assert c.get("/ha/state").status_code == 401
    assert c.get("/cub/summary").status_code == 401
    assert c.get("/timers").status_code == 200


def test_saved_switches_win(open_install, fresh_install, monkeypatch):
    monkeypatch.setattr(settings, "require_key_families", {"ha": False}, raising=False)
    c = _client()
    assert c.get("/ha/state").status_code != 401
    assert c.get("/cub/summary").status_code == 401


def test_satellites_leave_home_assistant_open(open_install, fresh_install, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    req = access.effective_required_families(settings, auth_state)
    assert req == {"ha": False, "events": False, "reader": True, "display": True}


def test_a_key_of_the_family_scope_passes(open_install, fresh_install, monkeypatch):
    from app.services import credentials
    monkeypatch.setattr(settings, "legacy_mirror", False, raising=False)
    _rec, key = credentials.mint("display", "Cub", "manual")
    assert _client().get("/cub/summary", headers={"X-API-Key": key}).status_code != 401


# --- The satellite gate -----------------------------------------------------------

@pytest.fixture
def satellite(open_install, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "remote_server_url", "http://server.test", raising=False)
    monkeypatch.setattr(settings, "upstream_api_key", "upstream-key", raising=False)
    from app import main

    async def _no_forward(*a, **k):
        return None
    return main


@pytest.mark.parametrize("server_has_password,gated", [(True, True), (False, False), (None, False)])
def test_recipe_forwards_follow_the_servers_password(satellite, monkeypatch, server_has_password,
                                                     gated):
    from app import main
    monkeypatch.setattr(settings, "fleet_relay", {"v": 1, "caps": ["ai"],
                                                  "server_has_password": server_has_password},
                        raising=False)
    monkeypatch.setattr(main, "_satellite_recipe_upstream", lambda path: "http://server.test"
                        if path.startswith("/recipes") else None)
    snap = access.AccessSnapshot(is_satellite=True, server_has_password=server_has_password,
                                 upstream_linked=True)
    assert main._gate_applies("/recipes/list", "GET", snap) is gated
    assert main._gate_applies("/setup/update", "POST", snap) is True
    assert main._gate_applies("/timers", "GET", snap) is True


@pytest.mark.parametrize("server_has_password,gated", [(True, True), (False, False), (None, True)])
def test_relayed_camera_feeds(satellite, server_has_password, gated):
    from app import main
    snap = access.AccessSnapshot(is_satellite=True, server_has_password=server_has_password,
                                 camera_relay_active=True)
    assert main._gate_applies("/ui/camera/0/snapshot", "GET", snap) is gated
    off = access.AccessSnapshot(is_satellite=True, server_has_password=server_has_password)
    assert main._gate_applies("/ui/camera/0/snapshot", "GET", off) is False


def test_the_gate_refuses_anonymous_home_callers(satellite):
    r = _client().post("/setup/update", json={})
    assert r.status_code == 403 and r.json()["detail"] == access.SATELLITE_GATE_MSG
    assert _client(base_url="http://localhost", peer=("127.0.0.1", 50000)).post(
        "/setup/update", json={}).status_code != 403


# --- The Docker gateway -------------------------------------------------------------

def test_the_gateway_item_once_per_episode(open_install, monkeypatch, items):
    monkeypatch.setattr(lan, "default_gateways", lambda: {"172.17.0.1"})
    monkeypatch.setattr(lan, "in_docker_bridge", lambda: True)
    c = _client(peer=("172.17.0.1", 50000))
    c.get("/timers")
    c.get("/timers")
    gw = [i for i in items if i["kind"] == "open_gateway_peer"]
    assert len(gw) == 1 and gw[0]["body"] == access.GATEWAY_HINT
    assert auth_state.gateway_peer_recent()
    assert not auth_state.gateway_peer_recent(now=time.time() + 31 * DAY)


@pytest.mark.parametrize("base_url", ["http://localhost:9284", "http://127.0.0.1:9284",
                                      "http://[::1]:9284"])
def test_a_browser_on_the_server_raises_no_gateway_item(open_install, monkeypatch, items,
                                                       base_url):
    """A browser on the server opening localhost goes through Docker's port
    forwarder and so arrives from the gateway. That is the owner at the
    server, not another container or an IPv6 visitor: no item, no notice."""
    monkeypatch.setattr(lan, "default_gateways", lambda: {"172.17.0.1"})
    monkeypatch.setattr(lan, "in_docker_bridge", lambda: True)
    assert _client(peer=("172.17.0.1", 50000), base_url=base_url).get(
        "/timers").status_code == 200
    assert not [i for i in items if i["kind"] == "open_gateway_peer"]
    assert not auth_state.gateway_peer_recent()


def test_the_gateway_hint_does_not_prescribe_a_made_change():
    assert "if it does not start with 0.0.0.0" in access.GATEWAY_HINT
    assert access.gateway_peer_is_unclear("192.168.1.5:9284") is True
    assert access.gateway_peer_is_unclear("localhost:9284") is False


def test_the_gateway_hint_on_unraid_asks_for_a_password():
    """Unraid's template publishes the port on every address and cannot be
    told otherwise, so its hint asks for a password instead of a compose edit."""
    assert access.unraid_runtime({"HOST_OS": "Unraid"}) is True
    assert access.unraid_runtime({}) is False
    assert access.gateway_hint({}) == access.GATEWAY_HINT
    hint = access.gateway_hint({"HOST_OS": "Unraid"})
    assert hint == access.GATEWAY_HINT_UNRAID
    assert "Settings, Security" in hint and "password" in hint
    assert "compose" not in hint.lower() and "0.0.0.0" not in hint
    assert "compose" in access.GATEWAY_HINT


def test_the_unraid_gateway_item_and_access(open_install, monkeypatch, items):
    """On Unraid the item carries the Unraid wording; the gateway visitor is
    handled exactly as on any other Docker install."""
    monkeypatch.setenv("HOST_OS", "Unraid")
    monkeypatch.setattr(lan, "default_gateways", lambda: {"172.17.0.1"})
    monkeypatch.setattr(lan, "in_docker_bridge", lambda: True)
    assert _client(peer=("172.17.0.1", 50000)).get("/timers").status_code == 200
    gw = [i for i in items if i["kind"] == "open_gateway_peer"]
    assert len(gw) == 1 and gw[0]["body"] == access.GATEWAY_HINT_UNRAID


def test_the_add_on_expects_home_assistant_on_its_gateway(open_install, monkeypatch, items):
    """In the Home Assistant add-on, Home Assistant calls from the add-on
    network's gateway, so that raises no item and no notice."""
    monkeypatch.setenv("PANTRY_RAIDER_ADDON", "1")
    monkeypatch.setattr(lan, "default_gateways", lambda: {"172.30.32.1"})
    monkeypatch.setattr(lan, "in_docker_bridge", lambda: True)
    assert _client(peer=("172.30.32.1", 50000)).get("/timers").status_code == 200
    assert not [i for i in items if i["kind"] == "open_gateway_peer"]
    assert not auth_state.gateway_peer_recent()


# --- Keyless home devices and the first password (round 2) ------------------------

def test_the_first_password_names_keyless_devices(open_install, items):
    c = _client()
    c.get("/ha/state", headers={"User-Agent": "HomeAssistant/2026.9 aiohttp/3.10"})
    c.get("/cub/summary", headers={"User-Agent": "esphome/2025.1"})
    c.post("/gadgets/esp-action", json={}, headers={"User-Agent": "ESPHome/2025.1"})
    assert not [i for i in items if i["kind"] == "keyless_devices_need_key"]
    r = c.post("/setup/save", json={"auth_password": "longpassword1", "auth_required": True})
    assert r.status_code == 200, r.text
    c.get("/health")
    got = [i for i in items if i["kind"] == "keyless_devices_need_key"]
    assert len(got) == 1 and got[0]["level"] == "warning"
    for label in ("Home Assistant", "Bandit Cubs and small displays", "ESP buttons and screens"):
        assert label in got[0]["body"]
    c.get("/health")
    assert len([i for i in items if i["kind"] == "keyless_devices_need_key"]) == 1


def test_no_item_without_keyless_devices(open_install, items):
    c = _client()
    c.post("/setup/save", json={"auth_password": "longpassword1", "auth_required": True})
    c.get("/health")
    assert not [i for i in items if i["kind"] == "keyless_devices_need_key"]


def test_a_password_from_the_environment_raises_it_once(open_install, items, monkeypatch):
    auth_state.record_keyless_ha("192.168.1.20")
    monkeypatch.setattr(settings, "auth_password", "hashed-from-env", raising=False)
    assert access.note_password_state_and_notify()
    assert not access.note_password_state_and_notify()
    assert len([i for i in items if i["kind"] == "keyless_devices_need_key"]) == 1


def test_state_created_with_a_password_never_raises_it(open_install, items, monkeypatch):
    monkeypatch.setattr(settings, "auth_password", "hashed", raising=False)
    auth_state.reset(auth_state.test_state(), persist=False)
    auth_state.record_keyless_ha("192.168.1.20")
    assert not access.note_password_state_and_notify()
    assert not items


def test_turned_away_devices_show_in_the_security_state(open_install, monkeypatch):
    from app.passwords import hash_secret
    auth_state.note_password_state(False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret("hunter2"), raising=False)
    access.note_password_state_and_notify()
    c = _client()
    assert c.get("/ha/state", headers={"User-Agent": "HomeAssistant/2026.9"}).status_code == 401
    c.post("/ui/login", data={"password": "hunter2"}, follow_redirects=False)
    state = c.get("/setup/security/state").json()
    assert state["keyless_refused"] == ["Home Assistant"]


def test_esphome_buttons_are_recorded(open_install):
    _client().post("/gadgets/esp-action", json={}, headers={"User-Agent": "esphome/2025.1"})
    assert auth_state.keyless_device_labels(30) == ["ESP buttons and screens"]
