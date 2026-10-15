""""Sign in with Google" on the device login page (FoodAssistant-cd34).

A Forager account created with Google has no password, so the mode=forager
email+password form cannot unlock the device for its owner. The login page
now offers the cloud's Google sign-in when the install is linked AND the
cloud says the unlock flow exists (the same /v1/meta capability source the
setup wizard uses, fetched server-side). Both legs ride the already-public
/ui/login path: ?google=start leaves for the cloud, ?code=... is the cloud's
return, redeemed at POST /v1/instance/verify-unlock.

Every cloud call is faked the same way the mode=forager login tests fake
verify-login, so these drive the real routes with a TestClient and no network.
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.passwords import hash_secret  # noqa: E402
from app.services import rate_limit  # noqa: E402

_PW = "kitchen-secret"
_META_ON = {"oauth_google": True, "google_unlock": True}


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    # Configured so the setup-redirect middleware serves the login route.
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "gk", raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret(_PW), raising=False)
    monkeypatch.setattr(settings, "viewer_password", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "local_totp_enabled", False, raising=False)
    monkeypatch.setattr(settings, "local_totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "tunnel_enabled", False, raising=False)
    monkeypatch.setattr(settings, "qr_public_url", "", raising=False)
    monkeypatch.setattr(settings, "cloud_instance_token", "", raising=False)
    monkeypatch.setattr(settings, "cloud_base_url", "https://cloud.test", raising=False)
    # A fresh capability cache and limiter each test, so state never leaks.
    from app.routers import ui
    monkeypatch.setattr(ui, "_google_meta_cache", {"ts": float("-inf"), "ok": False})
    rate_limit.login_guard.reset("testclient")
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        rate_limit.login_guard.reset("testclient")
        os.chdir(cwd)


def _link(monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_token", raising=False)


class _Resp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


class _FakeCloud:
    """Stands in for httpx.AsyncClient: one canned reply per verb, and a call
    log of (method, url, json_body)."""

    def __init__(self, get_resp=None, post_resp=None, calls=None):
        self._get = get_resp or _Resp(200, {})
        self._post = post_resp or _Resp(500, {})
        self.calls = calls if calls is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        self.calls.append(("GET", url, None))
        return self._get

    async def post(self, url, **kw):
        self.calls.append(("POST", url, kw.get("json")))
        return self._post


def _cloud(get_resp=None, post_resp=None, calls=None):
    return patch("app.routers.ui.httpx.AsyncClient",
                 side_effect=lambda *a, **k: _FakeCloud(get_resp, post_resp, calls))


# --- gating ------------------------------------------------------------------

def test_unlinked_install_shows_no_google_button_and_calls_no_cloud(client):
    calls = []
    with _cloud(calls=calls):
        body = client.get("/ui/login").text
    assert "Sign in with Google" not in body
    assert calls == []


def test_button_renders_when_linked_and_cloud_offers_unlock(client, monkeypatch):
    _link(monkeypatch)
    calls = []
    with _cloud(get_resp=_Resp(200, _META_ON), calls=calls):
        body = client.get("/ui/login").text
    assert "Sign in with Google" in body
    assert "google=start" in body
    assert ("GET", "https://cloud.test/v1/meta", None) in calls


def test_no_button_against_an_older_cloud(client, monkeypatch):
    # An older VPS answers /v1/meta without google_unlock: the flow cannot
    # finish there, so the button must not render and the set-a-password
    # guidance stays.
    _link(monkeypatch)
    with _cloud(get_resp=_Resp(200, {"oauth_google": True})):
        body = client.get("/ui/login").text
    assert "Sign in with Google" not in body
    assert "Set a password on the Forager site" in body


def test_no_button_when_cloud_unreachable(client, monkeypatch):
    _link(monkeypatch)

    class _Boom:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            raise httpx.ConnectError("no route")

    with patch("app.routers.ui.httpx.AsyncClient",
               side_effect=lambda *a, **k: _Boom()):
        body = client.get("/ui/login").text
    assert "Sign in with Google" not in body
    assert "Sign in with Forager" in body  # the page itself still renders


# --- the start leg -----------------------------------------------------------

def test_start_leaves_for_the_cloud_with_flow_unlock(client, monkeypatch):
    _link(monkeypatch)
    r = client.get("/ui/login?google=start", follow_redirects=False)
    assert r.status_code == 303
    target = urlsplit(r.headers["location"])
    assert f"{target.scheme}://{target.netloc}" == "https://cloud.test"
    assert target.path == "/auth/google/start"
    q = parse_qs(target.query)
    assert q["flow"] == ["unlock"]
    ret = urlsplit(q["return_url"][0])
    # The return address is this install's own origin, landing back on the
    # login page itself (the path the cloud appends ?code=... to).
    assert ret.netloc == "testserver"
    assert ret.path == "/ui/login"


def test_start_without_a_link_returns_to_login(client):
    r = client.get("/ui/login?google=start", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith("/ui/login")


# --- the return leg ----------------------------------------------------------

def test_return_happy_path_opens_the_session(client, monkeypatch):
    _link(monkeypatch)
    calls = []
    ok = _Resp(200, {"ok": True, "account_email": "dan@example.com"})
    with _cloud(post_resp=ok, calls=calls):
        r = client.get("/ui/login?code=ABC23456", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith("/ui/")
    posts = [c for c in calls if c[0] == "POST"]
    assert posts and posts[0][1].endswith("/v1/instance/verify-unlock")
    assert posts[0][2] == {"code": "ABC23456"}
    # The same session a password login opens: a protected page now answers.
    assert client.get("/setup", follow_redirects=False).status_code == 200


def test_return_mismatched_account_is_refused_with_a_clear_message(client, monkeypatch):
    _link(monkeypatch)
    with _cloud(post_resp=_Resp(401, {"error": "account_mismatch"})):
        r = client.get("/ui/login?code=ABC23456", follow_redirects=False)
    assert r.status_code == 401
    assert "not the one this kitchen is connected to" in r.text
    # No session was opened.
    assert client.get("/setup", follow_redirects=False).status_code in (401, 302, 303, 307)


def test_return_replayed_or_expired_code_is_refused(client, monkeypatch):
    _link(monkeypatch)
    with _cloud(post_resp=_Resp(401, {"error": "invalid_code"})):
        r = client.get("/ui/login?code=SPENT234", follow_redirects=False)
    assert r.status_code == 401
    assert "expired or was already used" in r.text
    assert client.get("/setup", follow_redirects=False).status_code in (401, 302, 303, 307)


def test_return_failures_feed_the_login_guard(client, monkeypatch):
    # Repeated bad codes count against the same limiter as bad passwords, and
    # a blocked request never even reaches the cloud.
    _link(monkeypatch)
    calls = []
    with _cloud(post_resp=_Resp(401, {"error": "invalid_code"}), calls=calls):
        for _ in range(rate_limit.login_guard.max_attempts):
            r = client.get("/ui/login?code=WRONG234", follow_redirects=False)
            assert r.status_code == 401
        cloud_calls_before = len(calls)
        r = client.get("/ui/login?code=WRONG234", follow_redirects=False)
    assert r.status_code == 429
    assert "too many" in r.text.lower()
    assert len(calls) == cloud_calls_before  # blocked before any round trip


def test_return_success_resets_the_login_guard(client, monkeypatch):
    _link(monkeypatch)
    with _cloud(post_resp=_Resp(401, {"error": "invalid_code"})):
        for _ in range(rate_limit.login_guard.max_attempts - 1):
            client.get("/ui/login?code=WRONG234", follow_redirects=False)
    ok = _Resp(200, {"ok": True, "account_email": "dan@example.com"})
    with _cloud(post_resp=ok):
        r = client.get("/ui/login?code=RIGHT234", follow_redirects=False)
    assert r.status_code == 303
    assert rate_limit.login_guard.blocked("testclient") is False


def test_return_with_cloud_unreachable_degrades_honestly(client, monkeypatch):
    _link(monkeypatch)

    class _Boom:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise httpx.ConnectError("no route")

    with patch("app.routers.ui.httpx.AsyncClient",
               side_effect=lambda *a, **k: _Boom()):
        r = client.get("/ui/login?code=ABC23456", follow_redirects=False)
    assert r.status_code == 502
    assert "could not be reached" in r.text.lower()


def test_return_code_on_an_unlinked_install_is_refused(client):
    calls = []
    with _cloud(calls=calls):
        r = client.get("/ui/login?code=ABC23456", follow_redirects=False)
    assert r.status_code == 400
    assert "not connected to Forager" in r.text
    assert all(c[0] != "POST" for c in calls)


def test_no_account_flag_shows_a_friendly_message(client, monkeypatch):
    _link(monkeypatch)
    with _cloud(get_resp=_Resp(200, _META_ON)):
        r = client.get("/ui/login?error=no-account")
    assert r.status_code == 200
    assert "No Forager account uses that Google address" in r.text


def test_authed_browser_is_bounced_home_before_any_google_leg(client, monkeypatch):
    _link(monkeypatch)
    client.post("/ui/login", data={"mode": "local", "password": _PW},
                follow_redirects=False)
    calls = []
    with _cloud(calls=calls):
        r = client.get("/ui/login?code=ABC23456", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith("/ui/")
    assert calls == []
