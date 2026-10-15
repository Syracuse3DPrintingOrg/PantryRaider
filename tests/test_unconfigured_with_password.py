"""An install with a password but no finished setup asks for the password.

A container template can set AUTH_PASSWORD before Grocy is ever connected.
The setup bypass used to serve the wizard and its saves to anyone while the
install was unconfigured, so a stranger on the LAN could turn the password
off or start two-factor (security review, Oct 2026). With a password set, the
bypass now needs a sign-in, except the read-only kitchen-screen status pages.
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
from app.services import access, rate_limit  # noqa: E402

_PW = "kitchen-secret"


@pytest.fixture
def app(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    # Grocy still at the template default: not configured yet.
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy:80", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "", raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret(_PW), raising=False)
    monkeypatch.setattr(settings, "viewer_password", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "local_totp_enabled", False, raising=False)
    monkeypatch.setattr(settings, "local_totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "tunnel_enabled", False, raising=False)
    rate_limit.login_guard.reset("192.168.1.77")
    from app.main import app as fastapi_app
    assert not settings.is_configured()
    try:
        yield fastapi_app
    finally:
        os.chdir(cwd)


def _lan(app):
    return TestClient(app, client=("192.168.1.77", 50000))


def test_anonymous_lan_caller_cannot_use_the_wizard(app):
    c = _lan(app)
    r = c.get("/setup", follow_redirects=False)
    assert r.status_code in (302, 303) and r.headers["location"].endswith("/ui/login")
    r = c.post("/setup/save", json={"auth_required": False}, follow_redirects=False)
    assert r.status_code in (401, 403)
    assert settings.auth_required is True
    for path in ("/setup/totp/generate", "/setup/first-run/reveal"):
        assert c.post(path, follow_redirects=False).status_code in (401, 403)


def test_signed_in_owner_still_gets_the_wizard(app):
    c = _lan(app)
    r = c.post("/ui/login", data={"password": _PW}, follow_redirects=False)
    assert r.status_code == 303, (r.headers, r.text[:300])
    r2 = c.get("/setup", follow_redirects=False)
    assert r2.status_code == 200, (r.headers, dict(c.cookies), r2.text[:300])


def test_kitchen_screen_status_pages_stay_open():
    assert "/ui/getting-ready/status" in access.SETUP_STATUS_PATHS
    assert "/setup/save" not in access.SETUP_STATUS_PATHS


def test_no_password_still_opens_the_wizard(app, monkeypatch):
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    assert _lan(app).get("/setup", follow_redirects=False).status_code == 200


def test_the_browser_that_sets_the_first_password_stays_in_the_wizard(app, monkeypatch):
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    mine, other = _lan(app), _lan(app)
    r = mine.post("/setup/save", json={"auth_password": "first-password-1"})
    assert r.json().get("ok") is not False
    assert settings.auth_password
    assert mine.get("/setup", follow_redirects=False).status_code == 200
    assert other.get("/setup", follow_redirects=False).status_code in (302, 303)
