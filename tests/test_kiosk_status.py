"""Consolidated kiosk status poll (FoodAssistant-us1i).

One GET (/kiosk/status) gathers every field the individual kiosk pollers read,
from the same sources and with the same per-field auth posture, so an idle
kiosk makes one request per tick instead of eight. These tests pin the
contract: the response shape on a server and a satellite, that the auth/bypass
posture matches the individual endpoints exactly (a viewer or unauthenticated
caller gets no more than it does today), that a satellite forwards the
fleet-owned fields and overlays the device-local ones, and that the browser
pollers now ride the shared loop and keep their cache-busters.

Pure logic: no Pi, host bridge, or network needed (the bridge calls degrade to
their off-Pi shapes and the satellite forward is stubbed).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.passwords import hash_secret  # noqa: E402

JS = SERVICE / "app" / "static" / "js"
TEMPLATES = SERVICE / "app" / "templates"


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #

@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        # client_host defaults to "testclient", NOT loopback, so the loopback
        # admin trust never masks the role checks under test.
        with TestClient(app) as c:
            yield c
    finally:
        os.chdir(cwd)


def _server(monkeypatch, admin="", viewer=""):
    """A configured server install (is_configured() true)."""
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "api_key", "", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", bool(admin), raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret(admin) if admin else "", raising=False)
    monkeypatch.setattr(settings, "viewer_password",
                        hash_secret(viewer) if viewer else "", raising=False)


def _login(client, password):
    return client.post("/ui/login", data={"password": password}, follow_redirects=False)


# --------------------------------------------------------------------------- #
# response shape (server)
# --------------------------------------------------------------------------- #

def test_returns_every_field_with_the_right_shape(client, monkeypatch):
    """Passwordless server (the middleware is a no-op, so this caller is admin,
    exactly as /setup is reachable by all on a passwordless install)."""
    _server(monkeypatch)
    j = client.get("/kiosk/status?since=999999999&kiosk=1&expiring=1").json()

    # viewer-legitimate fields, always present
    assert isinstance(j["timers"]["timers"], list)
    assert isinstance(j["events"]["events"], list) and "last_id" in j["events"]
    assert isinstance(j["scanner_mode"]["mode"], str)
    for k in ("pending", "actions", "alerts", "expiring"):
        assert isinstance(j["counts"][k], int)
    assert isinstance(j["server_time_epoch"], (int, float))

    # device telemetry, admin-gated: present for this admin caller, same shape
    # the host bridge returns off a Pi (the individual endpoints' clean shapes)
    assert j["activity"]["ok"] is True
    assert j["health"] == {"ok": True, "warnings": []}
    assert j["calibrate_pending"] is False
    assert j["nav_pending"] is False


def test_expiring_and_nav_are_opt_in(client, monkeypatch):
    """counts.expiring is the one Grocy-backed field, so it is only gathered
    when asked (Start page); nav_pending is only carried for a kiosk caller,
    because reading it consumes a one-shot flag."""
    _server(monkeypatch)
    plain = client.get("/kiosk/status").json()
    assert "expiring" not in plain.get("counts", {})
    assert "nav_pending" not in plain

    withflags = client.get("/kiosk/status?expiring=1&kiosk=1").json()
    assert "expiring" in withflags["counts"]
    assert "nav_pending" in withflags


def test_fields_come_from_the_same_sources_as_the_individual_endpoints(client, monkeypatch):
    """The merged values equal what the standalone endpoints return, proving the
    consolidated poll reads the same state, not a parallel copy."""
    _server(monkeypatch)
    j = client.get("/kiosk/status?expiring=1").json()
    assert j["timers"] == client.get("/timers").json()
    assert j["scanner_mode"] == client.get("/pending/scanner-mode").json()
    assert j["counts"]["pending"] == client.get("/pending/count").json()["count"]
    assert j["counts"]["actions"] == client.get("/action-items/count").json()["count"]
    assert j["counts"]["alerts"] == client.get("/events/count").json()["count"]


# --------------------------------------------------------------------------- #
# auth posture (must not widen exposure vs the individual endpoints)
# --------------------------------------------------------------------------- #

def test_unauthenticated_is_401_like_the_individual_endpoints(client, monkeypatch):
    """With a password set and no credentials, the consolidated poll 401s just
    like /timers does; it is a normal authenticated endpoint, not a bypass."""
    _server(monkeypatch, admin="hunter2")
    assert client.get("/timers").status_code == 401
    assert client.get("/kiosk/status").status_code == 401


def test_viewer_gets_kitchen_fields_but_not_device_telemetry(client, monkeypatch):
    """A viewer session reaches the poll (it is not admin-only) and gets the
    kitchen fields it can already read individually, but the /setup-gated device
    telemetry is OMITTED, which lands the viewer exactly where its individual
    device polls do today: a 403 there, nothing shown here."""
    _server(monkeypatch, admin="hunter2", viewer="kitchen")
    _login(client, "kitchen")
    r = client.get("/kiosk/status?kiosk=1&expiring=1")
    assert r.status_code == 200
    j = r.json()
    # kitchen-legitimate fields present (a viewer can hit these individually)
    assert "timers" in j and "events" in j and "counts" in j and "scanner_mode" in j
    # admin-gated device fields omitted, matching the 403 a viewer gets on /setup
    for k in ("activity", "health", "nav_pending", "calibrate_pending"):
        assert k not in j, f"{k} must be withheld from a viewer session"


def test_api_key_is_full_access(client, monkeypatch):
    """The X-API-Key path the satellite and Home Assistant use stays full
    access, so the device telemetry comes back for a keyed caller."""
    _server(monkeypatch, admin="hunter2")
    monkeypatch.setattr(settings, "api_key", "secret-key", raising=False)
    r = client.get("/kiosk/status", headers={"X-API-Key": "secret-key"})
    assert r.status_code == 200
    assert "activity" in r.json() and "health" in r.json()


def test_caller_is_admin_matrix(monkeypatch):
    """_caller_is_admin mirrors main.require_auth's /setup gate exactly."""
    from app.routers import kiosk_status as ks

    class FakeReq:
        def __init__(self, host=None, headers=None, session=None):
            self.client = type("C", (), {"host": host})() if host else None
            self.headers = headers or {}
            self.session = session or {}

    # passwordless: /setup is reachable by all today, so admin is True
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    assert ks._caller_is_admin(FakeReq()) is True

    monkeypatch.setattr(settings, "auth_password", hash_secret("x"), raising=False)
    monkeypatch.setattr(settings, "api_key", "K", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", "", raising=False)
    # loopback is always trusted
    assert ks._caller_is_admin(FakeReq(host="127.0.0.1")) is True
    # a valid API key is full access
    assert ks._caller_is_admin(FakeReq(headers={"X-API-Key": "K"})) is True
    # an admin session
    assert ks._caller_is_admin(FakeReq(session={"authed": True, "role": "admin"})) is True
    # a viewer session is NOT admin
    assert ks._caller_is_admin(FakeReq(session={"authed": True, "role": "viewer"})) is False
    # unauthenticated (password set, no creds) is NOT admin
    assert ks._caller_is_admin(FakeReq()) is False
    # a password-accepted-but-TOTP-pending session is NOT admin
    assert ks._caller_is_admin(
        FakeReq(session={"authed": True, "role": "admin", "totp_pending": True})) is False


# --------------------------------------------------------------------------- #
# one-shot nav flag semantics
# --------------------------------------------------------------------------- #

def test_nav_flag_is_consumed_once_and_only_by_a_kiosk_caller(client, monkeypatch):
    """The kiosk hand-off flag reads True once for a kiosk=1 poll and clears; a
    non-kiosk poll never touches it (no display to hand off)."""
    _server(monkeypatch)
    from app.routers import setup as setup_router
    setup_router._write_kiosk_nav_flag()

    # a non-kiosk poll must not report or consume the flag
    assert "nav_pending" not in client.get("/kiosk/status").json()
    # a kiosk poll reads it True exactly once, then it is cleared
    assert client.get("/kiosk/status?kiosk=1").json()["nav_pending"] is True
    assert client.get("/kiosk/status?kiosk=1").json()["nav_pending"] is False


# --------------------------------------------------------------------------- #
# satellite forwarding
# --------------------------------------------------------------------------- #

def _satellite(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "remote_server_url", "http://main.server", raising=False)
    monkeypatch.setattr(settings, "upstream_api_key", "up-key", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)


def _fwd_response(payload, status=200):
    """A forwarded upstream answer, the Response shape the per-endpoint seams
    return on a satellite (bytes body, not a dict)."""
    import json as _json
    from starlette.responses import Response
    return Response(content=_json.dumps(payload), status_code=status,
                    media_type="application/json")


def test_satellite_forwards_fleet_fields_and_keeps_events_local(client, monkeypatch):
    """A pi_remote answers the fleet-owned fields (timers, the pending/action/
    expiring counts, scanner mode) through the EXISTING per-endpoint forwards
    (which exist on any main-server version), and answers the device-local
    fields itself: its own events ring and alert count, and its own bridge/flags.
    The hand-off flag is never forwarded, so the server's flag is not consumed."""
    _satellite(monkeypatch)
    from app.routers import current_recipe as cr, pending as pd, action_items as ai, expiring as ex

    async def timers_fwd(request):
        return _fwd_response({"timers": [{"id": 7, "running": True}]})

    async def scanner_fwd(request):
        return _fwd_response({"mode": "consume", "label": "Use"})

    async def pending_fwd(request, db):
        return _fwd_response({"count": 3})

    async def actions_fwd(request, db):
        return _fwd_response({"count": 2})

    async def expiring_local(days=7):
        return {"ok": True, "count": 5}

    monkeypatch.setattr(cr, "get_timers", timers_fwd)
    monkeypatch.setattr(pd, "scanner_mode_get", scanner_fwd)
    monkeypatch.setattr(pd, "pending_count", pending_fwd)
    monkeypatch.setattr(ai, "count_items", actions_fwd)
    monkeypatch.setattr(ex, "get_expiring_count", expiring_local)

    j = client.get("/kiosk/status?expiring=1&kiosk=1").json()

    # fleet-owned fields taken from the forwarded per-endpoint answers
    assert j["timers"]["timers"] == [{"id": 7, "running": True}]
    assert j["scanner_mode"]["mode"] == "consume"
    assert j["counts"]["pending"] == 3
    assert j["counts"]["actions"] == 2
    assert j["counts"]["expiring"] == 5
    # alerts is LOCAL (this device's own events ring), not forwarded
    assert j["counts"]["alerts"] == 0
    # events come from the local ring, not the forward
    assert isinstance(j["events"]["events"], list)
    # device fields answered locally (off-Pi bridge shape); the satellite's own
    # nav flag is unset here -> False, never the server's
    assert j["activity"]["ok"] is True
    assert j["nav_pending"] is False


def test_satellite_degrades_to_local_when_the_main_server_is_down(client, monkeypatch):
    """A downed main server (the per-endpoint forwards return a 502 Response)
    omits the fleet fields, so the client keeps its last on-glass state rather
    than blanking, while the device-local fields still answer."""
    _satellite(monkeypatch)
    from app.routers import current_recipe as cr, pending as pd, action_items as ai

    async def down_timers(request):
        return _fwd_response({"detail": "The main server is not reachable."}, status=502)

    async def down_scanner(request):
        return _fwd_response({"detail": "down"}, status=502)

    async def down_pending(request, db):
        return _fwd_response({"detail": "down"}, status=502)

    async def down_actions(request, db):
        return _fwd_response({"detail": "down"}, status=502)

    monkeypatch.setattr(cr, "get_timers", down_timers)
    monkeypatch.setattr(pd, "scanner_mode_get", down_scanner)
    monkeypatch.setattr(pd, "pending_count", down_pending)
    monkeypatch.setattr(ai, "count_items", down_actions)

    j = client.get("/kiosk/status").json()
    # fleet fields omitted (client leaves last state), local fields present
    assert "timers" not in j
    assert "scanner_mode" not in j
    assert "events" in j
    assert j["counts"]["alerts"] == 0  # local ring still answers
    assert "activity" in j             # local bridge still answers


# --------------------------------------------------------------------------- #
# the browser side: pollers ride the shared loop, cache-busters preserved
# --------------------------------------------------------------------------- #

def test_shared_loop_exists_and_has_the_right_poll_hygiene():
    src = (JS / "kiosk-status.js").read_text()
    # It is THE consolidated URL, relative like the sibling pollers (no leading
    # slash, so an ingress prefix survives).
    assert "'kiosk/status?'" in src
    assert "'/kiosk/status" not in src
    # It carries the events cursor and the two opt-in flags.
    assert "since=" in src and "&kiosk=1" in src and "&expiring=1" in src
    # One chained-setTimeout loop, never setInterval; skips hidden; backs off.
    assert "setInterval(" not in src
    assert "setTimeout(poll" in src
    assert "document.hidden" in src
    assert "visibilitychange" in src
    assert "BACKOFF_MAX_MS = 30000" in src
    assert "Math.min(delay * 2, BACKOFF_MAX_MS)" in src
    # The public API the consumers use.
    assert "window.PRKioskStatus" in src
    assert "subscribe:" in src and "last:" in src


def test_base_and_start_load_the_shared_loop_cache_busted():
    for name in ("base.html", "start.html"):
        src = (TEMPLATES / name).read_text()
        m = re.search(r'src="static/js/kiosk-status\.js(\?[^"]*)"', src)
        assert m, f"{name} must load kiosk-status.js"
        assert "v=" in m.group(1), f"{name} loads kiosk-status.js with no ?v="


def test_kiosk_consumers_subscribe_to_the_shared_loop():
    """Every steady kiosk poller rides the consolidated loop, with one
    deliberate exception: the on-screen event channel. Event feedback drives a
    physical control's response (a NeoKey press jumps the kiosk to the scan
    screen), which has to feel immediate, so ha-events keeps its own fast
    /events/poll (the cheapest endpoint, an in-memory ring read) rather than the
    2 to 4s shared loop. The expensive surfaces stay consolidated."""
    for name in ("timer-chips.js", "presence-indicator.js"):
        src = (JS / name).read_text()
        assert "window.PRKioskStatus" in src, f"{name} does not ride the shared loop"
        assert "PRKioskStatus.subscribe" in src, f"{name} does not subscribe"
    # ha-events keeps its OWN fast poll for low-latency physical-control
    # feedback, so it must NOT be folded back onto the slow shared loop.
    hae = (JS / "ha-events.js").read_text()
    assert "events/poll" in hae, "ha-events lost its dedicated events poll"
    assert "PRKioskStatus.subscribe" not in hae, \
        "ha-events should keep its own fast poll, not ride the slow shared loop"
    # base.html inline pollers (counts, health, calibrate, navigate) subscribe.
    base = (TEMPLATES / "base.html").read_text()
    assert base.count("PRKioskStatus.subscribe") >= 4
    # the scanner-mode reconcile on the Manage Pantry page rides it too.
    add = (TEMPLATES / "add.html").read_text()
    assert "PRKioskStatus.subscribe" in add
    # the Start page tiles/pills/timer faces ride it too.
    start = (TEMPLATES / "start.html").read_text()
    assert start.count("PRKioskStatus.subscribe") >= 2
    # the screensaver reads the shared snapshot instead of its own second fetch.
    saver = (JS / "screensaver.js").read_text()
    assert "PRKioskStatus.last()" in saver
