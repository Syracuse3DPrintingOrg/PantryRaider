"""Fewer sign-ins, real sign-out, and the two-factor choices (FoodAssistant-9g6os,
-6lbsa, -qtm5f, -2w35t, -s48wm).

Drives the real app with a TestClient: the pr_session cookie and its move from
the old ``session`` name, the sliding window, Keep me signed in, signing out
one browser or a copied cookie, two-factor at home or only from outside, the
trusted-browser cookie, and the Forager code step keeping the password out of
the page.
"""
import json
import os
import sys
import time
from base64 import b64encode
from pathlib import Path
from unittest.mock import patch

import itsdangerous
import pytest
from fastapi.testclient import TestClient

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.passwords import hash_secret  # noqa: E402
from app import totp as local_totp  # noqa: E402
from app import session_cookie as sc  # noqa: E402
from app.services import sessions  # noqa: E402

_PW = "kitchen-secret"
_PUBLIC = "https://home.forager.pantryraider.app"
_PUBLIC_HOST = "home.forager.pantryraider.app"


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "gk", raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret(_PW), raising=False)
    monkeypatch.setattr(settings, "viewer_password", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "local_totp_enabled", False, raising=False)
    monkeypatch.setattr(settings, "local_totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "local_totp_recovery", [], raising=False)
    monkeypatch.setattr(settings, "local_totp_scope", "always", raising=False)
    monkeypatch.setattr(settings, "tunnel_enabled", False, raising=False)
    monkeypatch.setattr(settings, "qr_public_url", _PUBLIC, raising=False)
    monkeypatch.setattr(settings, "cloud_instance_token", "", raising=False)
    monkeypatch.setattr(settings, "signin_news_until", "", raising=False)
    sessions.reset()
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        sessions.wait_idle()
        os.chdir(cwd)


def _signed_in(c) -> bool:
    return c.get("/setup", follow_redirects=False).status_code == 200


def _sign_in(c, **extra):
    r = c.post("/ui/login", data={"mode": "local", "password": _PW, **extra},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    return r


def _forge(data: dict, age: float = 0.0, name: str = "pr_session"):
    """A cookie value as this app signs it, signed ``age`` seconds ago."""
    signer = itsdangerous.TimestampSigner(str(settings.secret_key))
    with patch.object(itsdangerous.TimestampSigner, "get_timestamp",
                      lambda self: int(time.time() - age)):
        return signer.sign(b64encode(json.dumps(data).encode())).decode()


def _admin(**kw):
    from app.services import auth_state
    return {"authed": True, "role": "admin", "session_gen": auth_state.session_gen(), **kw}


def _cookies(r) -> list[str]:
    return r.headers.get_list("set-cookie")


# --- The cookie ---------------------------------------------------------------

def test_sign_in_sets_pr_session_not_session(client):
    r = _sign_in(client)
    names = [c.split("=", 1)[0] for c in _cookies(r)]
    assert "pr_session" in names and "session" not in names
    assert _signed_in(client)


def test_old_session_cookie_is_moved_over_without_signing_out(client):
    client.cookies.set("session", _forge(_admin(), name="session"))
    r = client.get("/setup", follow_redirects=False)
    assert r.status_code == 200
    set_cookies = _cookies(r)
    assert any(c.startswith("pr_session=") for c in set_cookies)
    assert any(c.startswith("session=null") and "1970" in c for c in set_cookies)


def test_another_apps_session_cookie_is_left_alone(client):
    client.cookies.set("session", "some-other-app-value")
    r = client.get("/ui/login", follow_redirects=False)
    assert not any(c.startswith("session=") for c in _cookies(r))


def test_cookie_is_refreshed_once_a_day_while_in_use(client):
    client.cookies.set("pr_session", _forge(_admin(sid="s1"), age=2 * 86400))
    r = client.get("/setup", follow_redirects=False)
    assert r.status_code == 200
    assert any(c.startswith("pr_session=") for c in _cookies(r))
    r = client.get("/setup", follow_redirects=False)
    assert not any(c.startswith("pr_session=") for c in _cookies(r))


def test_idle_for_31_days_is_signed_out(client):
    client.cookies.set("pr_session", _forge(_admin(sid="s1"), age=31 * 86400))
    assert not _signed_in(client)


def test_one_year_cap(client):
    client.cookies.set("pr_session", _forge(_admin(sid="s1", created=time.time() - 366 * 86400)))
    assert not _signed_in(client)


def test_unticked_keep_me_signed_in_is_a_browser_session(client):
    r = _sign_in(client, remember_choice="1")
    cookie = next(c for c in _cookies(r) if c.startswith("pr_session="))
    assert "Max-Age" not in cookie
    client.cookies.clear()
    client.cookies.set("pr_session", _forge(_admin(sid="s1", remember=False), age=13 * 3600))
    assert not _signed_in(client)


def test_ticked_keep_me_signed_in_lasts(client):
    r = _sign_in(client, remember_choice="1", remember="1")
    cookie = next(c for c in _cookies(r) if c.startswith("pr_session="))
    assert "Max-Age=2592000" in cookie


def test_secure_flag_behind_a_trusted_https_proxy():
    https_proxy = {"x-forwarded-proto": "https"}
    assert sc.secure_request({"scheme": "https", "client": ("203.0.113.9", 1)}, {})
    assert sc.secure_request({"scheme": "http", "client": ("127.0.0.1", 1)}, https_proxy)
    # A visitor cannot claim HTTPS for itself.
    assert not sc.secure_request({"scheme": "http", "client": ("203.0.113.9", 1)}, https_proxy)
    assert not sc.secure_request({"scheme": "http", "client": ("127.0.0.1", 1)}, {})


# --- Signing out ----------------------------------------------------------------

def test_sign_out_ends_a_copied_cookie_too(client):
    _sign_in(client)
    client.get("/setup")  # the first signed-in request puts it on the list
    copied = client.cookies.get("pr_session")
    assert client.post("/ui/logout", follow_redirects=False).status_code == 303
    client.cookies.clear()
    client.cookies.set("pr_session", copied)
    assert not _signed_in(client)


def test_a_link_on_another_site_cannot_sign_you_out(client):
    _sign_in(client)
    r = client.get("/ui/logout", headers={"sec-fetch-site": "cross-site"},
                   follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert _signed_in(client)
    client.get("/ui/logout", headers={"sec-fetch-site": "same-origin"})
    assert not _signed_in(client)


def test_sign_out_one_browser_from_settings(client, monkeypatch):
    other = TestClient(client.app)
    _sign_in(other)
    other.get("/setup", headers={"user-agent": "Mozilla/5.0 (iPhone) Safari/605"})
    _sign_in(client)
    listing = client.get("/setup/signins").json()
    phone = next(r for r in listing["sessions"] if not r["current"])
    assert phone["browser"] == "Safari on iPhone"
    assert client.post("/setup/signins/revoke", json={"id": phone["id"]}).json()["ok"]
    assert not _signed_in(other)
    assert _signed_in(client)


def test_a_fresh_sign_in_gets_a_new_id(client):
    _sign_in(client)
    client.get("/setup")
    before = client.cookies.get("pr_session")
    _sign_in(client)
    client.cookies.clear()
    client.cookies.set("pr_session", before)
    assert not _signed_in(client)


# --- Two-factor: when to ask, and trusted browsers ---------------------------

def _two_factor(monkeypatch, scope="always"):
    secret = local_totp.generate_totp_secret()
    monkeypatch.setattr(settings, "local_totp_secret", secret, raising=False)
    monkeypatch.setattr(settings, "local_totp_enabled", True, raising=False)
    monkeypatch.setattr(settings, "local_totp_scope", scope, raising=False)
    return secret


def test_only_from_outside_skips_the_code_at_home(client, monkeypatch):
    _two_factor(monkeypatch, "outside")
    _sign_in(client)
    assert _signed_in(client)


def test_only_from_outside_still_asks_from_outside(client, monkeypatch):
    _two_factor(monkeypatch, "outside")
    monkeypatch.setattr(settings, "tunnel_enabled", True, raising=False)
    r = client.post("/ui/login", data={"mode": "local", "password": _PW},
                    headers={"host": _PUBLIC_HOST}, follow_redirects=False)
    assert r.status_code == 200 and "authenticator" in r.text.lower()


def test_trusted_browser_skips_the_code_next_time(client, monkeypatch):
    secret = _two_factor(monkeypatch)
    client.post("/ui/login", data={"mode": "local", "password": _PW})
    r = client.post("/ui/login", data={"totp_code": local_totp.totp_now(secret),
                                       "trust_choice": "1", "trust": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert any(c.startswith("pr_trusted=") for c in _cookies(r))
    client.post("/ui/logout")
    _sign_in(client)  # straight in: no code step
    assert _signed_in(client)


def test_unticked_trust_sets_no_cookie(client, monkeypatch):
    secret = _two_factor(monkeypatch)
    client.post("/ui/login", data={"mode": "local", "password": _PW})
    r = client.post("/ui/login", data={"totp_code": local_totp.totp_now(secret),
                                       "trust_choice": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert not any(c.startswith("pr_trusted=") for c in _cookies(r))


def test_trust_ends_when_everyone_is_signed_out(client, monkeypatch):
    from app.services import auth_state
    secret = _two_factor(monkeypatch)
    client.post("/ui/login", data={"mode": "local", "password": _PW})
    client.post("/ui/login", data={"totp_code": local_totp.totp_now(secret),
                                   "trust_choice": "1", "trust": "1"})
    auth_state.bump_session_gen()
    client.cookies.delete("pr_session")
    r = client.post("/ui/login", data={"mode": "local", "password": _PW}, follow_redirects=False)
    assert r.status_code == 200 and "authenticator" in r.text.lower()


def test_asking_less_often_needs_proof(client, monkeypatch):
    _sign_in(client)
    _two_factor(monkeypatch)
    monkeypatch.setattr(type(settings), "save",
                        lambda self, d: [setattr(self, k, v) for k, v in d.items()], raising=False)
    d = client.post("/setup/totp/scope", json={"scope": "outside", "credential": "nope"}).json()
    assert not d["ok"]
    d = client.post("/setup/totp/scope", json={"scope": "outside", "credential": _PW}).json()
    assert d["ok"] and settings.local_totp_scope == "outside"
    d = client.post("/setup/totp/scope", json={"scope": "always"}).json()
    assert d["ok"]


# --- The sign-in page -------------------------------------------------------------

class _Resp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


class _FakeCloud:
    def __init__(self, replies, calls):
        self._replies = replies
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        self._calls.append(kw.get("json"))
        return self._replies.pop(0)


def test_forager_code_step_keeps_the_password_out_of_the_page(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "tok", raising=False)
    monkeypatch.setattr(settings, "cloud_base_url", "https://cloud.test", raising=False)
    calls = []
    replies = [_Resp(401, {"error": "totp_required"}), _Resp(200, {"ok": True, "mfa": True})]
    with patch("app.routers.ui.httpx.AsyncClient",
               side_effect=lambda *a, **k: _FakeCloud(replies, calls)):
        r = client.post("/ui/login", data={"mode": "forager", "email": "dan@example.com",
                                           "fpass": "hunter2222"})
        assert r.status_code == 401
        assert "hunter2222" not in r.text
        assert 'name="fcode"' in r.text
        r = client.post("/ui/login", data={"mode": "forager", "email": "dan@example.com",
                                           "fcode": "123456"}, follow_redirects=False)
    assert r.status_code == 303
    assert calls[1] == {"email": "dan@example.com", "password": "hunter2222", "totp": "123456"}


def test_other_ways_are_folded_away(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "tok", raising=False)
    page = client.get("/ui/login").text
    assert "Other ways to sign in" in page
    assert "Keep me signed in" in page


def test_signin_news_only_for_installs_that_had_a_password(monkeypatch):
    from app.services import signin_news
    saved = {}
    monkeypatch.setattr(settings, "signin_news_checked", False, raising=False)
    monkeypatch.setattr(settings, "signin_news_until", "", raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(type(settings), "save", lambda self, d: saved.update(d), raising=False)
    signin_news.seed(settings)
    assert saved == {"signin_news_checked": True}
    saved.clear()
    monkeypatch.setattr(settings, "auth_password", "x", raising=False)
    signin_news.seed(settings)
    assert saved["signin_news_checked"] and saved["signin_news_until"]
    monkeypatch.setattr(settings, "signin_news_until", saved["signin_news_until"], raising=False)
    assert signin_news.showing(settings)
    monkeypatch.setattr(settings, "signin_news_checked", True, raising=False)
    saved.clear()
    signin_news.seed(settings)
    assert saved == {}


def test_signin_news_shows_on_the_page(client, monkeypatch):
    monkeypatch.setattr(settings, "signin_news_until", "2999-01-01T00:00:00+00:00", raising=False)
    assert "Signing in has changed" in client.get("/ui/login").text


@pytest.mark.parametrize("path", ["/apple-touch-icon.png", "/apple-touch-icon-precomposed.png",
                                  "/favicon.ico"])
def test_root_icons_are_public(client, path):
    r = client.get(path)
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/")
