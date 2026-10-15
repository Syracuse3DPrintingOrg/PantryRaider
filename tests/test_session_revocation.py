"""Browser sessions can be revoked (security review, Oct 2026).

The session cookie used to read just {"authed": true, "role": "admin"}, so a
copy of it kept working for its whole 30 days through a password change, a
two-factor change and a sign-out elsewhere. Sessions now carry the generation
they were opened in, and a password or two-factor change, or Sign out
everywhere else, moves the generation on.
"""
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.passwords import hash_secret  # noqa: E402
from app import totp as local_totp  # noqa: E402
from app.services import auth_state, rate_limit  # noqa: E402

_PW = "kitchen-secret"


@pytest.fixture
def app(monkeypatch, tmp_path):
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
    monkeypatch.setattr(settings, "tunnel_enabled", False, raising=False)
    monkeypatch.setattr(settings, "cloud_instance_token", "", raising=False)
    rate_limit.login_guard.reset("testclient")
    from app.main import app as fastapi_app
    try:
        yield fastapi_app
    finally:
        rate_limit.login_guard.reset("testclient")
        os.chdir(cwd)


def _signed_in(app):
    c = TestClient(app)
    r = c.post("/ui/login", data={"password": _PW}, follow_redirects=False)
    assert r.status_code == 303
    assert c.get("/setup", follow_redirects=False).status_code == 200
    return c


def _locked_out(c) -> bool:
    return c.get("/setup", follow_redirects=False).status_code in (302, 303, 307, 401)


def test_sign_out_everywhere_else_keeps_only_this_browser(app):
    mine, other = _signed_in(app), _signed_in(app)
    assert mine.post("/setup/security/signout-everywhere").json()["ok"] is True
    assert mine.get("/setup", follow_redirects=False).status_code == 200
    assert _locked_out(other)
    # The signed-out browser lands on the sign-in page, not a redirect loop.
    r = other.get("/ui/login", follow_redirects=False)
    assert r.status_code == 200


def test_a_copied_cookie_dies_with_a_password_change(app):
    mine = _signed_in(app)
    stolen = TestClient(app, cookies=dict(mine.cookies))
    assert stolen.get("/setup", follow_redirects=False).status_code == 200
    r = mine.post("/setup/save", json={"auth_password": "a-new-password-1",
                                       "current_password": _PW})
    assert r.json().get("ok") is not False
    assert mine.get("/setup", follow_redirects=False).status_code == 200
    assert _locked_out(stolen)


def test_two_factor_changes_sign_out_others(app):
    mine, other = _signed_in(app), _signed_in(app)
    secret = local_totp.generate_totp_secret()
    d = mine.post("/setup/totp/verify",
                  json={"secret": secret, "code": local_totp.totp_now(secret)}).json()
    assert d["ok"] is True
    assert mine.get("/setup", follow_redirects=False).status_code == 200
    assert _locked_out(other)


def test_replacing_two_factor_needs_proof(app, monkeypatch):
    old = local_totp.generate_totp_secret()
    monkeypatch.setattr(settings, "local_totp_secret", old, raising=False)
    monkeypatch.setattr(settings, "local_totp_enabled", True, raising=False)
    c = TestClient(app)
    c.post("/ui/login", data={"password": _PW}, follow_redirects=False)
    c.post("/ui/login", data={"totp_code": local_totp.totp_now(old)}, follow_redirects=False)
    new = local_totp.generate_totp_secret()
    d = c.post("/setup/totp/verify",
               json={"secret": new, "code": local_totp.totp_now(new)}).json()
    assert d["ok"] is False
    assert settings.local_totp_secret == old
    d = c.post("/setup/totp/verify",
               json={"secret": new, "code": local_totp.totp_now(new),
                     "credential": _PW}).json()
    assert d["ok"] is True


def test_a_session_from_before_generations_still_works():
    # An update must not sign everyone out: no stamp reads as generation 1.
    assert auth_state.session_is_current({}) is (auth_state.session_gen() == 1)
