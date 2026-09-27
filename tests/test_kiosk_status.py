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

import asyncio
import json
import time
import os
import re
import shutil
import subprocess
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
    j = client.get("/kiosk/status?since=999999999&kiosk=1&expiring=1&scanner=1"
                   "&activity=1").json()

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
    plain = client.get("/kiosk/status?scanner=1").json()
    assert "expiring" not in plain.get("counts", {})
    assert "nav_pending" not in plain

    # scanner_mode is opt-in for the same reason: only Manage Pantry renders it,
    # and on a satellite gathering it is a forward to the main server, so the
    # pages that never show it must not pay for it every tick.
    no_scanner = client.get("/kiosk/status").json()
    assert "scanner_mode" not in no_scanner
    assert "scanner_mode" in client.get("/kiosk/status?scanner=1").json()

    withflags = client.get("/kiosk/status?expiring=1&kiosk=1").json()
    assert "expiring" in withflags["counts"]
    assert "nav_pending" in withflags


def test_fields_come_from_the_same_sources_as_the_individual_endpoints(client, monkeypatch):
    """The merged values equal what the standalone endpoints return, proving the
    consolidated poll reads the same state, not a parallel copy."""
    _server(monkeypatch)
    j = client.get("/kiosk/status?expiring=1&scanner=1").json()
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
    assert client.get("/kiosk/status?scanner=1").status_code == 401


def test_viewer_gets_kitchen_fields_but_not_device_telemetry(client, monkeypatch):
    """A viewer session reaches the poll (it is not admin-only) and gets the
    kitchen fields it can already read individually, but the /setup-gated device
    telemetry is OMITTED, which lands the viewer exactly where its individual
    device polls do today: a 403 there, nothing shown here."""
    _server(monkeypatch, admin="hunter2", viewer="kitchen")
    _login(client, "kitchen")
    r = client.get("/kiosk/status?kiosk=1&expiring=1&scanner=1&activity=1")
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
    r = client.get("/kiosk/status?activity=1", headers={"X-API-Key": "secret-key"})
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
    assert "nav_pending" not in client.get("/kiosk/status?scanner=1").json()
    # a kiosk poll reads it True exactly once, then it is cleared
    assert client.get("/kiosk/status?kiosk=1&scanner=1").json()["nav_pending"] is True
    assert client.get("/kiosk/status?kiosk=1&scanner=1").json()["nav_pending"] is False


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

    j = client.get("/kiosk/status?expiring=1&kiosk=1&scanner=1&activity=1").json()

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

    j = client.get("/kiosk/status?scanner=1&activity=1").json()
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
    # A request that never answers is abandoned (behavior pinned below).
    assert "REQUEST_TIMEOUT_MS = 15000" in src and "new AbortController()" in src
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
    # The shared page pollers (counts, health, calibrate, navigate) subscribe.
    # They were inline in base.html; they live in static/js/base-page.js now
    # so a navigation reuses the cached file instead of re-parsing them.
    page = (JS / "base-page.js").read_text()
    assert page.count("PRKioskStatus.subscribe") >= 4
    # the scanner-mode reconcile on the Manage Pantry page rides it too (its
    # page logic lives in a static file now, not inline in add.html).
    add = (JS / "manage-pantry.js").read_text()
    assert "PRKioskStatus.subscribe" in add
    # the Start page tiles/pills/timer faces ride it too.
    start = (TEMPLATES / "start.html").read_text()
    assert start.count("PRKioskStatus.subscribe") >= 2
    # the screensaver reads the shared snapshot instead of its own second fetch.
    saver = (JS / "screensaver.js").read_text()
    assert "PRKioskStatus.last()" in saver


# --------------------------------------------------------------------------- #
# the host-bridge activity slice is opt-in, and both consumers must claim it
# --------------------------------------------------------------------------- #

def test_activity_costs_nothing_until_a_surface_asks_for_it(client, monkeypatch):
    """Reading activity opens a fresh connection to the host bridge on every
    tick, and only two surfaces consume it. An ordinary open tab must not pay
    for it several times a minute for a field it throws away."""
    _server(monkeypatch)   # passwordless server: this caller is admin
    assert "activity" not in client.get("/kiosk/status").json()
    assert client.get("/kiosk/status?activity=1").json()["activity"]["ok"] is True


def test_the_bridge_is_not_touched_without_the_opt_in(client, monkeypatch):
    """The point of the flag is the call that does not happen, so pin the call
    itself rather than the field: a plain poll must never reach the bridge."""
    _server(monkeypatch)   # passwordless server: this caller is admin
    from app.routers import setup as setup_router
    calls = []

    async def _spy():
        calls.append(1)
        return {"ok": True}

    monkeypatch.setattr(setup_router, "kiosk_activity_state", _spy)
    client.get("/kiosk/status?kiosk=1&expiring=1&scanner=1")
    assert calls == [], "a poll with no activity opt-in still hit the host bridge"
    client.get("/kiosk/status?activity=1")
    assert calls == [1]


def test_both_activity_consumers_claim_the_field():
    """The failure mode here is silent: gate the field server-side and a
    consumer that never asks for it just reads undefined forever, so the
    presence dot stops lighting and the screensaver stops noticing activity on
    other surfaces, with nothing in any log. Pin both claims.

    The screensaver is the subtle one: it rides `PRKioskStatus.last()` instead
    of subscribing for its data, so its claim exists purely to turn the field
    on and would look like dead code to anyone tidying up.
    """
    presence = (JS / "presence-indicator.js").read_text()
    assert "wants: ['activity']" in presence, \
        "presence-indicator must ask for activity or its dot goes dark"

    saver = (JS / "screensaver.js").read_text()
    assert "wants: ['activity']" in saver, \
        "screensaver must claim activity even though it reads last(), not the cb"
    assert "claimActivity()" in saver, "the claim must actually be invoked"

    loop = (JS / "kiosk-status.js").read_text()
    assert "wantFlags().activity" in loop and "'&activity=1'" in loop, \
        "the shared loop must translate the claim into the query flag"



# --------------------------------------------------------------------------- #
# the satellite poll goes upstream once, not five times in a row
# --------------------------------------------------------------------------- #

def test_satellite_forwards_go_out_together_not_one_after_another(client, monkeypatch):
    """Every forward is a Wi-Fi round trip to the main server. Awaited in
    sequence, a cold-cache poll paid five of them back to back, which on the
    Bandit's link (6 to 70 ms each) was most of the poll interval spent
    waiting. Gathered, it pays one.

    Deterministic rather than timed: each stub records when it started and
    finished. If all five were in flight at once, the latest start is still
    earlier than the earliest finish. Serial awaits would have each start
    after the previous finish.
    """
    _satellite(monkeypatch)
    from app.routers import current_recipe as cr, pending as pd, action_items as ai, expiring as ex

    starts: list[float] = []
    ends: list[float] = []

    def stub(payload):
        async def _f(*args, **kwargs):
            starts.append(time.perf_counter())
            await asyncio.sleep(0.05)
            ends.append(time.perf_counter())
            return _fwd_response(payload)
        return _f

    monkeypatch.setattr(cr, "get_timers", stub({"timers": []}))
    monkeypatch.setattr(pd, "scanner_mode_get", stub({"mode": "inventory"}))
    monkeypatch.setattr(pd, "pending_count", stub({"count": 1}))
    monkeypatch.setattr(ai, "count_items", stub({"count": 2}))
    monkeypatch.setattr(ex, "get_expiring_count", stub({"count": 3}))

    j = client.get("/kiosk/status?expiring=1&scanner=1").json()

    assert len(starts) == 5 and len(ends) == 5, "all five forwards must still run"
    assert max(starts) < min(ends), (
        "the forwards ran one after another; they must be gathered")
    # And the answer is the same one the serial code produced.
    assert j["counts"] == {"pending": 1, "actions": 2, "expiring": 3, "alerts": 0}
    assert j["scanner_mode"]["mode"] == "inventory" and "timers" in j


def test_one_failed_forward_still_leaves_the_others_intact(client, monkeypatch):
    """Omit-on-failure survives the gather: a forward that blows up drops its
    own field and nothing else, exactly as the serial try/except did."""
    _satellite(monkeypatch)
    from app.routers import current_recipe as cr, pending as pd, action_items as ai, expiring as ex

    async def ok(payload):
        return _fwd_response(payload)

    async def boom(*args, **kwargs):
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(cr, "get_timers", boom)
    monkeypatch.setattr(pd, "scanner_mode_get", lambda request: ok({"mode": "consume"}))
    monkeypatch.setattr(pd, "pending_count", lambda request, db: ok({"count": 4}))
    monkeypatch.setattr(ai, "count_items", boom)
    monkeypatch.setattr(ex, "get_expiring_count", lambda days: ok({"count": 5}))

    j = client.get("/kiosk/status?expiring=1&scanner=1").json()
    assert "timers" not in j
    assert j["counts"] == {"pending": 4, "expiring": 5, "alerts": 0}
    assert j["scanner_mode"]["mode"] == "consume"


def test_screensaver_never_fetches_the_bridge_directly_when_the_shared_loop_exists():
    """A fresh document always starts with an empty PRKioskStatus.last(), so
    a fallback that fires whenever last() is empty fires on every navigation.
    That was a direct /setup/kiosk/activity fetch, a host-bridge round trip
    measured at 460 ms on a Pi 4, on every page load of every kiosk, for a
    value the shared poll (which the saver now claims) was about to deliver.

    The rule: when window.PRKioskStatus exists, the saver waits for the shared
    slice and returns; the direct fetch is reachable only when there is no
    shared loop on the page at all.
    """
    src = (JS / "screensaver.js").read_text()
    body = src[src.index("var pollExternalActivity"):src.index("setInterval(pollExternalActivity")]
    guard = body.index("if (window.PRKioskStatus)")
    ret = body.index("return;", guard)
    direct = body.index("fetch('setup/kiosk/activity'")
    assert guard < ret < direct, (
        "the direct bridge fetch must sit after an unconditional return inside "
        "the window.PRKioskStatus branch, so it never runs when the shared loop exists")
    # And the claim that makes the shared poll carry the slice is still made first.
    assert body.index("claimActivity()") < guard


# --------------------------------------------------------------------------- #
# the real forwards share one request, and a stuck poll cannot freeze the page
# --------------------------------------------------------------------------- #

def _cold_forward_caches():
    """Empty the satellite's forward micro-caches, the way a page's first poll
    finds them. They are module-level, so a test that fills them empties them
    again on the way out and never answers a later test's forward."""
    from app.routers import action_items as ai, current_recipe as cr, pending as pd
    cr._timers_cache.invalidate()
    pd._count_fwd_cache.invalidate()
    ai._count_fwd_cache.invalidate()


def test_satellite_first_poll_answers_with_every_forward_cold(monkeypatch, tmp_path):
    """The satellite tests above stub the forwarding handlers, so they cannot
    see how the real ones share the incoming request. Every real forward read
    the request body, and a body nobody has read yet cannot be read by two
    forwards at once: under the app's middleware the second reader waits for
    the client to hang up, which a polling kiosk never does. A page's first
    poll finds every micro-cache cold, so it never answered, and the timer
    chips, badges and scanner tabs froze with it.

    The real handlers run here; only the upstream client is swapped for a
    stand-in main server, and the call is bounded so a regression fails with a
    TimeoutError instead of hanging the suite.
    """
    _satellite(monkeypatch)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.chdir(SERVICE)
    import httpx
    from app.main import app
    from app.routers import action_items as ai, current_recipe as cr, pending as pd

    answers = {
        "/timers": {"timers": [{"id": 7, "running": True}]},
        "/pending/scanner-mode": {"mode": "consume", "label": "Use"},
        "/pending/count": {"count": 3},
        "/action-items/count": {"count": 2},
    }
    seen: list[tuple[str, str, str]] = []

    def main_server(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path, req.headers.get("X-API-Key", "")))
        if req.url.path in answers:
            return httpx.Response(200, json=answers[req.url.path])
        return httpx.Response(404, json={"detail": "Not Found"})

    async def first_poll():
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(main_server))
        for module in (cr, pd, ai):
            monkeypatch.setattr(module, "_fwd_client", upstream)
        # Cold, the way a page's first poll always finds them.
        _cold_forward_caches()
        kiosk = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 50000)),
            base_url="http://satellite.test")
        try:
            return await asyncio.wait_for(kiosk.get("/kiosk/status?scanner=1"), 5)
        finally:
            await kiosk.aclose()
            await upstream.aclose()
            _cold_forward_caches()

    r = asyncio.run(first_poll())

    assert r.status_code == 200
    assert sorted(path for _, path, _ in seen) == sorted(answers), (
        "every fleet field must have been asked of the main server")
    assert all(method == "GET" and key == "up-key" for method, _, key in seen)
    j = r.json()
    assert j["timers"] == answers["/timers"]
    assert j["scanner_mode"] == answers["/pending/scanner-mode"]
    assert j["counts"]["pending"] == 3 and j["counts"]["actions"] == 2


def test_gathered_fields_may_read_the_body_at_the_same_moment(monkeypatch, tmp_path):
    """The satellite poll reads the request's (empty) body itself before it
    gathers the fleet fields, so every gathered handler that reads it after
    that gets the stored copy at once. The stand-ins here all read it, the way
    every forward once did (the audit forward still does), so a field added to
    the gather later can read the body without bringing the hang back."""
    _satellite(monkeypatch)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.chdir(SERVICE)
    import httpx
    from app.main import app
    from app.routers import action_items as ai, current_recipe as cr, pending as pd

    bodies: list[bytes] = []

    def reads_the_body(payload):
        async def handler(request, db=None):
            bodies.append(await request.body())
            return _fwd_response(payload)
        return handler

    monkeypatch.setattr(cr, "get_timers", reads_the_body({"timers": []}))
    monkeypatch.setattr(pd, "scanner_mode_get",
                        reads_the_body({"mode": "consume", "label": "Use"}))
    monkeypatch.setattr(pd, "pending_count", reads_the_body({"count": 3}))
    monkeypatch.setattr(ai, "count_items", reads_the_body({"count": 2}))

    async def poll():
        kiosk = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 50000)),
            base_url="http://satellite.test")
        try:
            return await asyncio.wait_for(kiosk.get("/kiosk/status?scanner=1"), 5)
        finally:
            await kiosk.aclose()

    r = asyncio.run(poll())

    assert r.status_code == 200
    assert bodies == [b""] * 4, "every gathered handler read the same empty body"
    j = r.json()
    assert j["timers"] == {"timers": []}
    assert j["scanner_mode"]["mode"] == "consume"
    assert j["counts"]["pending"] == 3 and j["counts"]["actions"] == 2


def test_a_main_server_that_goes_quiet_cannot_hold_back_the_poll(monkeypatch, tmp_path):
    """The kiosk gives up on a poll after 15s, and a forward to a main server
    that has stopped answering can take 20s to fail. Waiting on it would lose
    the whole poll, this device's own events and alert count with it. The
    satellite drops such a field at its own deadline instead, so the poll
    still answers in time and the page keeps the dropped field's last value."""
    _satellite(monkeypatch)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.chdir(SERVICE)
    import httpx
    from app.main import app
    from app.routers import (action_items as ai, current_recipe as cr,
                             kiosk_status as ks, pending as pd)
    monkeypatch.setattr(ks, "_FIELD_DEADLINE_SECS", 1.0, raising=False)

    answers = {
        "/timers": {"timers": [{"id": 7, "running": True}]},
        "/pending/scanner-mode": {"mode": "consume", "label": "Use"},
        "/action-items/count": {"count": 2},
    }

    async def main_server(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/pending/count":
            await asyncio.sleep(30)  # took the request and never answers
        if req.url.path in answers:
            return httpx.Response(200, json=answers[req.url.path])
        return httpx.Response(404, json={"detail": "Not Found"})

    async def poll():
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(main_server))
        for module in (cr, pd, ai):
            monkeypatch.setattr(module, "_fwd_client", upstream)
        _cold_forward_caches()
        kiosk = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 50000)),
            base_url="http://satellite.test")
        try:
            return await asyncio.wait_for(kiosk.get("/kiosk/status?scanner=1"), 5)
        finally:
            await kiosk.aclose()
            await upstream.aclose()
            _cold_forward_caches()

    r = asyncio.run(poll())

    assert r.status_code == 200
    j = r.json()
    assert "pending" not in j["counts"], "the quiet field is dropped, not waited on"
    assert j["counts"]["actions"] == 2 and j["counts"]["alerts"] == 0
    assert j["timers"] == answers["/timers"]
    assert j["scanner_mode"] == answers["/pending/scanner-mode"]
    assert "events" in j


def test_a_quiet_grocy_cannot_hold_back_the_start_page_poll(monkeypatch, tmp_path):
    """The same deadline on a server. The Start page's expiring count pulls
    Grocy, whose client allows 15s, the very moment the kiosk gives up, so a
    Grocy that stopped answering would cost the whole poll. It now costs only
    that count, which the page keeps showing from its last answer."""
    _server(monkeypatch)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    from app.routers import (action_items as ai, expiring as ex,
                             kiosk_status as ks, pending as pd)
    monkeypatch.setattr(ks, "_FIELD_DEADLINE_SECS", 1.0, raising=False)

    async def quiet_grocy(days=7):
        await asyncio.sleep(30)
        return {"ok": True, "count": 9}

    async def pending_count(request, db):
        return {"count": 3}

    async def count_items(request, db):
        return {"count": 2}

    monkeypatch.setattr(ex, "get_expiring_count", quiet_grocy)
    monkeypatch.setattr(pd, "pending_count", pending_count)
    monkeypatch.setattr(ai, "count_items", count_items)

    out = asyncio.run(asyncio.wait_for(ks._gather_local(
        None, 999999999, want_expiring=True, want_scanner=False, admin=False,
        kiosk=False, db=None), 5))

    assert "expiring" not in out["counts"], "the quiet count is dropped, not waited on"
    assert out["counts"]["pending"] == 3 and out["counts"]["actions"] == 2
    assert "timers" in out and "events" in out


@pytest.mark.parametrize("module_name, subpath, upstream_path, payload", [
    ("pending", "/scan", "/pending/scan", b'{"barcode": "078000035483"}'),
    ("action_items", "/5/snooze", "/action-items/5/snooze", b'{"hours": 24}'),
    ("current_recipe", "/timers", "/timers", b'{"label": "Rice", "seconds": 900}'),
    ("audit", "/scan", "/audit/scan", b'{"barcode": "078000035483"}'),
])
def test_forwards_leave_a_get_body_alone_and_pass_a_post_body_on(
        monkeypatch, module_name, subpath, upstream_path, payload):
    """The forwards the satellite poll gathers never wait on the body of a
    GET, which has none (waiting on it is what hung the poll), while a POST
    still reaches the main server with its body intact: a scan taken on a
    satellite that lost its body would arrive with no barcode."""
    _satellite(monkeypatch)
    import importlib
    import httpx
    from starlette.requests import Request
    module = importlib.import_module(f"app.routers.{module_name}")
    sent: list[tuple[str, str, bytes]] = []

    def main_server(req: httpx.Request) -> httpx.Response:
        sent.append((req.method, req.url.path, req.content))
        return httpx.Response(200, json={"ok": True})

    async def forward(method: str, body: bytes):
        reads: list[str] = []

        async def receive():
            reads.append(method)
            return {"type": "http.request", "body": body, "more_body": False}

        scope = {"type": "http", "method": method, "path": "/", "query_string": b"",
                 "headers": [(b"content-type", b"application/json")]}
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(main_server))
        monkeypatch.setattr(module, "_fwd_client", upstream)
        try:
            resp = await module._forward(Request(scope, receive), subpath)
        finally:
            await upstream.aclose()
        return resp.status_code, reads

    assert asyncio.run(forward("GET", b"")) == (200, []), (
        "a GET forward must not wait on a request body")
    assert asyncio.run(forward("POST", payload)) == (200, ["POST"])
    assert sent == [("GET", upstream_path, b""), ("POST", upstream_path, payload)]


_LOOP_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
let now = 0, nextId = 1, timers = [];
const calls = [];
let answering = false;      // until flipped, the server never answers at all
const sandbox = {
  window: {},
  document: { hidden: false, readyState: 'complete', addEventListener() {} },
  localStorage: { getItem() { return null; } },
  AbortController,
  setTimeout(fn, ms) { const id = nextId++; timers.push({ id, at: now + (ms || 0), fn }); return id; },
  clearTimeout(id) { timers = timers.filter(t => t.id !== id); },
  fetch(url, opts) {
    const signal = opts && opts.signal;
    calls.push({ url, signal });
    if (answering) {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ counts: {} }) });
    }
    return new Promise((resolve, reject) => {
      if (signal) signal.addEventListener('abort', () => {
        const e = new Error('aborted'); e.name = 'AbortError'; reject(e);
      });
    });
  },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(__KIOSK_STATUS_JS__, 'utf8'), sandbox);
const flush = () => new Promise(r => setImmediate(r));
async function advance(ms) {
  const end = now + ms;
  for (;;) {
    await flush();
    timers.sort((a, b) => a.at - b.at || a.id - b.id);
    if (!timers.length || timers[0].at > end) break;
    const t = timers.shift();
    now = t.at;
    t.fn();
  }
  now = end;
  await flush();
}
const aborted = c => !!(c && c.signal && c.signal.aborted);
(async () => {
  let painted = 0;
  sandbox.window.PRKioskStatus.subscribe(() => { painted++; }, { interval: 4000 });
  await flush();
  const out = { at_start: calls.length };
  await advance(14000);
  out.at_14s = { calls: calls.length, first_aborted: aborted(calls[0]) };
  await advance(10000);
  out.at_24s = { calls: calls.length, first_aborted: aborted(calls[0]) };
  answering = true;
  await advance(60000);
  out.painted = painted;
  out.answered_then_aborted = calls.slice(2).filter(aborted).length;
  console.log(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_a_poll_that_never_answers_cannot_freeze_the_loop():
    """The loop allows one request at a time, and fetch has no deadline of its
    own, so a single request the server never answered held the in-flight
    flag forever: no further poll, and every surface riding the loop (timer
    chips, badges, scanner tabs, health, the screensaver wake) stopped for good.
    Drive the real kiosk-status.js on a fake clock against a server that never
    answers: the request is given up on at 15s, and after the backoff the loop
    polls again and paints once the server recovers."""
    script = _LOOP_HARNESS.replace("__KIOSK_STATUS_JS__",
                                   json.dumps(str(JS / "kiosk-status.js")))
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    assert res["at_start"] == 1
    # Still waiting at 14s: a slow answer from a busy Pi is not a hang.
    assert res["at_14s"] == {"calls": 1, "first_aborted": False}
    # Given up on at 15s, then the doubled backoff (8s) and a fresh poll.
    assert res["at_24s"] == {"calls": 2, "first_aborted": True}, (
        "a request that never answers must be abandoned so the loop goes on")
    # Once the server answers again the surfaces are painted as before, and
    # an answered request's deadline is cleared rather than firing later.
    assert res["painted"] >= 1
    assert res["answered_then_aborted"] == 0
