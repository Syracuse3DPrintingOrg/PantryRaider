"""The reworked login flow (FoodAssistant-x1ty): the local password path with a
2FA challenge, the internet-forcing rule, and the "Sign in with Forager" path.

The cloud verify-login round-trip is faked, so these drive the real /ui/login
routes with a TestClient and no network.
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.passwords import hash_secret  # noqa: E402
from app import totp as local_totp  # noqa: E402

_PW = "kitchen-secret"
_PUBLIC = "https://home.forager.pantryraider.app"
_PUBLIC_HOST = "home.forager.pantryraider.app"


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    # Configured so the setup-redirect middleware serves the login route rather
    # than bouncing an unconfigured install to the wizard.
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "gk", raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret(_PW), raising=False)
    monkeypatch.setattr(settings, "viewer_password", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    # Default: no 2FA, no tunnel (LAN).
    monkeypatch.setattr(settings, "local_totp_enabled", False, raising=False)
    monkeypatch.setattr(settings, "local_totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "local_totp_recovery", [], raising=False)
    monkeypatch.setattr(settings, "tunnel_enabled", False, raising=False)
    monkeypatch.setattr(settings, "qr_public_url", _PUBLIC, raising=False)
    monkeypatch.setattr(settings, "cloud_instance_token", "", raising=False)
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


class _Resp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


class _FakeCloud:
    def __init__(self, resp, calls):
        self._resp = resp
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        self._calls.append((url, None))
        return self._resp

    async def post(self, url, **kw):
        self._calls.append((url, kw.get("json")))
        return self._resp


# --- local password path ---------------------------------------------------

def test_lan_password_without_2fa_signs_in(client):
    r = client.post("/ui/login", data={"mode": "local", "password": _PW},
                    follow_redirects=False)
    assert r.status_code == 303
    # A protected page is now reachable with the session cookie.
    assert client.get("/setup", follow_redirects=False).status_code == 200


def test_internet_password_without_2fa_is_refused(client, monkeypatch):
    monkeypatch.setattr(settings, "tunnel_enabled", True, raising=False)
    r = client.post("/ui/login", data={"mode": "local", "password": _PW},
                    headers={"host": _PUBLIC_HOST}, follow_redirects=False)
    assert r.status_code == 401
    assert "second factor" in r.text.lower()
    # No session was opened.
    assert client.get("/setup", follow_redirects=False).status_code in (401, 302, 303, 307)


def test_password_with_2fa_challenges_then_signs_in(client, monkeypatch):
    secret = local_totp.generate_totp_secret()
    monkeypatch.setattr(settings, "local_totp_secret", secret, raising=False)
    monkeypatch.setattr(settings, "local_totp_enabled", True, raising=False)
    # Step 1: password lands on the code step, not a session.
    r1 = client.post("/ui/login", data={"mode": "local", "password": _PW},
                     follow_redirects=False)
    assert r1.status_code == 200
    assert "authenticator" in r1.text.lower()
    assert client.get("/setup", follow_redirects=False).status_code in (401, 302, 303, 307)
    # Step 2: the right code finishes the sign-in.
    r2 = client.post("/ui/login",
                     data={"totp_code": local_totp.totp_now(secret)},
                     follow_redirects=False)
    assert r2.status_code == 303
    assert client.get("/setup", follow_redirects=False).status_code == 200


def test_wrong_2fa_code_is_rejected(client, monkeypatch):
    secret = local_totp.generate_totp_secret()
    monkeypatch.setattr(settings, "local_totp_secret", secret, raising=False)
    monkeypatch.setattr(settings, "local_totp_enabled", True, raising=False)
    client.post("/ui/login", data={"mode": "local", "password": _PW},
                follow_redirects=False)
    r = client.post("/ui/login", data={"totp_code": "000000"},
                    follow_redirects=False)
    assert r.status_code == 401
    assert client.get("/setup", follow_redirects=False).status_code in (401, 302, 303, 307)


def test_recovery_code_completes_the_challenge(client, monkeypatch):
    secret = local_totp.generate_totp_secret()
    monkeypatch.setattr(settings, "local_totp_secret", secret, raising=False)
    monkeypatch.setattr(settings, "local_totp_enabled", True, raising=False)
    monkeypatch.setattr(settings, "local_totp_recovery",
                        local_totp.hash_recovery_codes(["ABCD-2345"]), raising=False)
    client.post("/ui/login", data={"mode": "local", "password": _PW},
                follow_redirects=False)
    r = client.post("/ui/login", data={"totp_code": "abcd2345"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/setup", follow_redirects=False).status_code == 200


# --- Forager path ----------------------------------------------------------

def _link(monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_token", raising=False)
    monkeypatch.setattr(settings, "cloud_base_url", "https://cloud.test", raising=False)


def test_login_page_offers_forager_when_linked(client, monkeypatch):
    _link(monkeypatch)
    # The login page now checks the cloud's capabilities server-side when the
    # install is linked; fake that call so the test stays offline.
    from app.routers import ui
    monkeypatch.setattr(ui, "_google_meta_cache", {"ts": float("-inf"), "ok": False})
    resp = _Resp(200, {})
    with patch("app.routers.ui.httpx.AsyncClient",
               side_effect=lambda *a, **k: _FakeCloud(resp, [])):
        assert "Sign in with Forager" in client.get("/ui/login").text


def test_forager_login_success_opens_session(client, monkeypatch):
    _link(monkeypatch)
    calls = []
    resp = _Resp(200, {"ok": True, "account_email": "dan@example.com"})
    with patch("app.routers.ui.httpx.AsyncClient",
               side_effect=lambda *a, **k: _FakeCloud(resp, calls)):
        r = client.post("/ui/login",
                        data={"mode": "forager", "email": "dan@example.com",
                              "fpass": "hunter2222"},
                        follow_redirects=False)
    assert r.status_code == 303
    assert calls and calls[0][0].endswith("/v1/instance/verify-login")
    assert client.get("/setup", follow_redirects=False).status_code == 200


def test_forager_login_prompts_for_totp(client, monkeypatch):
    _link(monkeypatch)
    resp = _Resp(401, {"error": "totp_required"})
    with patch("app.routers.ui.httpx.AsyncClient",
               side_effect=lambda *a, **k: _FakeCloud(resp, [])):
        r = client.post("/ui/login",
                        data={"mode": "forager", "email": "dan@example.com",
                              "fpass": "hunter2222"},
                        follow_redirects=False)
    assert r.status_code == 401
    assert "authenticator" in r.text.lower()
    # No session yet.
    assert client.get("/setup", follow_redirects=False).status_code in (401, 302, 303, 307)


def test_forager_login_wrong_code_message(client, monkeypatch):
    _link(monkeypatch)
    resp = _Resp(401, {"error": "totp_invalid"})
    with patch("app.routers.ui.httpx.AsyncClient",
               side_effect=lambda *a, **k: _FakeCloud(resp, [])):
        r = client.post("/ui/login",
                        data={"mode": "forager", "email": "dan@example.com",
                              "fpass": "hunter2222", "fcode": "000000"},
                        follow_redirects=False)
    assert r.status_code == 401
    assert "did not match" in r.text.lower()


def test_forager_login_bad_credentials_is_generic(client, monkeypatch):
    _link(monkeypatch)
    resp = _Resp(401, {"error": "invalid_credentials"})
    with patch("app.routers.ui.httpx.AsyncClient",
               side_effect=lambda *a, **k: _FakeCloud(resp, [])):
        r = client.post("/ui/login",
                        data={"mode": "forager", "email": "dan@example.com",
                              "fpass": "wrong"},
                        follow_redirects=False)
    assert r.status_code == 401
    assert "was not right" in r.text.lower()


def test_forager_login_degrades_when_cloud_unreachable(client, monkeypatch):
    _link(monkeypatch)
    import httpx

    class _Boom:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise httpx.ConnectError("no route")

    with patch("app.routers.ui.httpx.AsyncClient",
               side_effect=lambda *a, **k: _Boom()):
        r = client.post("/ui/login",
                        data={"mode": "forager", "email": "dan@example.com",
                              "fpass": "hunter2222"},
                        follow_redirects=False)
    assert r.status_code == 502
    assert "could not be reached" in r.text.lower()
