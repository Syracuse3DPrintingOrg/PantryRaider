"""The kitchen screen signs itself in (/ui/kiosk/launch and /ui/kiosk/relaunch)."""
from __future__ import annotations

import asyncio
import re
import secrets
import time
from pathlib import Path

import pytest

from app.config import settings
from app.passwords import hash_secret
from app.services import access, auth_state, local_agents

SERVICE = Path(__file__).resolve().parents[1] / "service"


@pytest.fixture
def install(bridge_token, monkeypatch):
    """A configured server with a password whose old local trust has ended."""
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret("hunter2"), raising=False)
    monkeypatch.setattr(settings, "kiosk_screen_is_admin", True, raising=False)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True, raising=False)
    auth_state.retire("admin")
    return bridge_token


def _ticket(token, t=None):
    return local_agents.make_ticket(token, int(t or time.time()), secrets.token_hex(16))


def test_a_good_ticket_opens_a_kiosk_session(install, loopback_client):
    assert loopback_client.get("/timers").status_code == 401
    r = loopback_client.get("/ui/kiosk/launch",
                            params={"ticket": _ticket(install), "next": "/ui/add?x=1"},
                            follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/add?x=1&kiosk=1"
    assert "session=" in r.headers.get("set-cookie", "")
    assert loopback_client.get("/timers").status_code == 200
    # The screen opens Settings while it counts as the owner.
    assert loopback_client.get("/setup/security/state").status_code == 200
    assert auth_state.load()["agents"]["screen"]["last_ticket"]


@pytest.mark.parametrize("next_path", ["https://evil.example/", "//evil.example", "javascript:alert(1)",
                                       "", "/ javascript:x"])
def test_open_redirects_land_home(install, loopback_client, next_path):
    r = loopback_client.get("/ui/kiosk/launch", params={"ticket": "x", "next": next_path},
                            follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/?kiosk=1"


def test_bad_tickets_fall_through(install, loopback_client, lan_client):
    from fastapi.testclient import TestClient
    from app.main import app
    cases = [
        (loopback_client, {}, "garbage"),
        (loopback_client, {}, _ticket("not-the-token")),
        (lan_client, {}, _ticket(install)),
        (loopback_client, {"X-Forwarded-For": "203.0.113.9"}, _ticket(install)),
        (TestClient(app, client=("127.0.0.1", 50000), base_url="http://evil.example"), {},
         _ticket(install)),
    ]
    for client, headers, ticket in cases:
        r = client.get("/ui/kiosk/launch", params={"ticket": ticket}, headers=headers,
                       follow_redirects=False)
        assert r.status_code == 303
        assert "session=" not in r.headers.get("set-cookie", "")
        assert client.get("/timers", headers=headers).status_code == 401


def test_the_probe_touches_nothing(install, loopback_client):
    r = loopback_client.get("/ui/kiosk/launch", params={"probe": "1"}, follow_redirects=False)
    assert r.status_code == 204
    assert "set-cookie" not in r.headers


def test_the_kiosk_cookie_counts_only_on_this_device(install, loopback_client, lan_client,
                                                     kiosk_login):
    kiosk_login(loopback_client)
    lan_client.cookies.set("session", loopback_client.cookies.get("session"))
    assert lan_client.get("/timers").status_code == 401
    assert loopback_client.get("/timers").status_code == 200


def test_signing_the_screen_out(install, loopback_client, kiosk_login):
    kiosk_login(loopback_client)
    assert loopback_client.get("/timers").status_code == 200
    auth_state.bump_kiosk_gen()
    assert loopback_client.get("/timers").status_code == 401


def test_one_cookie_refresh_per_new_day(install, loopback_client, kiosk_login, monkeypatch):
    kiosk_login(loopback_client)
    r = loopback_client.get("/timers")
    assert "set-cookie" not in r.headers
    import datetime as _dt

    class _Tomorrow(_dt.date):
        @classmethod
        def today(cls):
            return _dt.date(2099, 1, 2)

    monkeypatch.setattr(_dt, "date", _Tomorrow)
    r = loopback_client.get("/timers")
    assert "session=" in r.headers.get("set-cookie", "")
    r = loopback_client.get("/timers")
    assert "set-cookie" not in r.headers


def test_unconfigured_lands_on_setup(bridge_token, monkeypatch, loopback_client):
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    from app.services import readiness
    monkeypatch.setattr(readiness, "gate_possible", lambda: False)
    r = loopback_client.get("/ui/kiosk/launch", params={"ticket": _ticket(bridge_token)},
                            follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/?kiosk=1"
    r = loopback_client.get("/ui/?kiosk=1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup?kiosk=1"


def test_the_pin_is_still_asked(install, monkeypatch, loopback_client):
    monkeypatch.setattr(type(settings), "pin_lock_active", lambda self: True, raising=False)
    monkeypatch.setattr(settings, "kiosk_readonly_when_locked", False, raising=False)
    r = loopback_client.get("/ui/kiosk/launch", params={"ticket": _ticket(install)},
                            follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/?kiosk=1"
    r = loopback_client.get("/ui/?kiosk=1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/ui/pin")


# --- Relaunch ---------------------------------------------------------------------

class _FakeBridge:
    def __init__(self, calls):
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        self.calls.append(url)
        return type("R", (), {"status_code": 200})()


def test_relaunch_is_local_and_limited(install, monkeypatch, loopback_client, lan_client):
    calls = []
    monkeypatch.setattr(local_agents.bridge, "bridge_client", lambda **k: _FakeBridge(calls))
    r = lan_client.post("/ui/kiosk/relaunch")
    assert r.status_code == 403 and r.json()["error"] == access.RELAUNCH_LOCAL_MSG
    r = loopback_client.post("/ui/kiosk/relaunch")
    assert r.json() == {"ok": True, "restarting": True}
    assert calls == ["http://127.0.0.1:9299/kiosk/restart"]
    r = loopback_client.post("/ui/kiosk/relaunch")
    assert r.json()["restarting"] is False                      # within 10 minutes
    assert len(calls) == 1


def test_relaunch_is_capped_at_three_a_day(monkeypatch):
    calls = []
    monkeypatch.setattr(local_agents.bridge, "bridge_client", lambda **k: _FakeBridge(calls))
    base = time.time()
    clock = {"t": base}
    monkeypatch.setattr(local_agents.time, "time", lambda: clock["t"])
    results = []
    for i in range(5):
        clock["t"] = base + i * 601
        results.append(asyncio.run(local_agents.request_kiosk_relaunch("test")))
    if len({time.localtime(base + i * 601).tm_yday for i in range(5)}) == 1:
        assert results == [True, True, True, False, False]


def test_a_signed_out_screen_restarts_itself(install, monkeypatch, loopback_client):
    started = []

    async def _relaunch(reason=""):
        started.append(reason)
        return True

    monkeypatch.setattr(local_agents, "request_kiosk_relaunch", _relaunch)
    monkeypatch.setattr(local_agents, "kiosk_installed", lambda: True)
    browser = {"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) Chrome/120"}
    r = loopback_client.get("/ui/", headers=browser, follow_redirects=False)
    assert r.status_code == 303
    assert started == ["refused"]
    loopback_client.get("/ui/", headers={"User-Agent": "curl/8"}, follow_redirects=False)
    assert started == ["refused"]


# --- The screen without Settings ------------------------------------------------

def _kiosk_calls() -> set:
    root = SERVICE / "app"
    found = set()
    files = list((root / "static" / "js").glob("*.js")) + [
        f for f in (root / "templates").glob("*.html") if f.name != "setup.html"]
    for f in files:
        for m in re.finditer(r"['\"`/]((?:setup|admin)/[a-z0-9_/-]+)", f.read_text(errors="replace")):
            path = "/" + m.group(1).rstrip("/")
            if path in ("/setup/update", "/setup/save", "/setup/grocy/repair"):
                continue
            found.add(path)
    return found


def test_the_device_routes_cover_what_the_screen_calls():
    calls = _kiosk_calls()
    assert len(calls) >= 8, calls
    allowed = {p for _m, p in access.KIOSK_DEVICE_ROUTES}
    assert calls <= allowed, sorted(calls - allowed)


def test_a_screen_without_settings(install, monkeypatch, loopback_client, kiosk_login):
    monkeypatch.setattr(settings, "kiosk_screen_is_admin", False, raising=False)
    kiosk_login(loopback_client)
    assert loopback_client.get("/timers").status_code == 200
    r = loopback_client.get("/setup", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/?kiosk=1"
    r = loopback_client.post("/setup/save", json={})
    assert r.status_code == 403 and r.json()["detail"] == access.VIEWER_MSG
    for method, path in access.KIOSK_DEVICE_ROUTES:
        assert access.allows_raw("kiosk", method, path, kiosk_is_admin=False)
    assert not access.allows_raw("kiosk", "POST", "/ha/connect", kiosk_is_admin=False)
    assert not access.allows_raw("kiosk", "GET", "/setup/security/state", kiosk_is_admin=False)
