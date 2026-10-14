"""Login/PIN throttling and the proxy-aware second factor (FoodAssistant-7svb).

Nothing rate-limited the local password, TOTP, or PIN before, so an internet-
exposed login was brute-forceable; and the internet second factor only fired for
the built-in Forager tunnel, so a bare password went through as a single factor
behind a different reverse proxy. These cover both.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.passwords import hash_secret  # noqa: E402
from app.services import rate_limit  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret("admin-pw"), raising=False)
    monkeypatch.setattr(settings, "viewer_password", "", raising=False)
    monkeypatch.setattr(settings, "tunnel_enabled", False, raising=False)
    monkeypatch.setattr(settings, "qr_public_url", "", raising=False)
    monkeypatch.setattr(settings, "local_totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "totp_secret", "", raising=False)
    # Fresh limiters each test so counts do not leak between them.
    rate_limit.login_guard.reset("testclient")
    rate_limit.pin_guard.reset("testclient")
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        rate_limit.login_guard.reset("testclient")
        rate_limit.pin_guard.reset("testclient")
        os.chdir(cwd)


def test_wrong_password_is_throttled_after_max_attempts(client):
    # The first max_attempts wrong guesses answer 401; the next is locked out.
    for _ in range(rate_limit.login_guard.max_attempts):
        r = client.post("/ui/login", data={"password": "nope"},
                        follow_redirects=False)
        assert r.status_code == 401
    r = client.post("/ui/login", data={"password": "nope"}, follow_redirects=False)
    assert r.status_code == 429
    assert "too many" in r.text.lower()


def test_successful_login_resets_the_counter(client):
    for _ in range(rate_limit.login_guard.max_attempts - 1):
        client.post("/ui/login", data={"password": "nope"}, follow_redirects=False)
    # A correct login clears the strikes.
    r = client.post("/ui/login", data={"password": "admin-pw"}, follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    # A fresh run of wrong guesses is allowed again (counter was reset).
    r = client.post("/ui/login", data={"password": "nope"}, follow_redirects=False)
    assert r.status_code == 401


def test_bare_password_refused_over_a_non_forager_proxy(client):
    # Tunnel off, but a forwarding header means a reverse proxy is in front, so
    # the login is treated as internet and the single-factor password is refused
    # (FoodAssistant-7svb). The correct password is used, so this is not a
    # brute-force lockout but the second-factor requirement.
    r = client.post("/ui/login", data={"password": "admin-pw"},
                    headers={"X-Forwarded-For": "203.0.113.9"},
                    follow_redirects=False)
    assert r.status_code == 401
    assert "second factor" in r.text.lower() or "forager" in r.text.lower()


def test_bare_password_ok_on_direct_lan(client, monkeypatch):
    # Same correct password, no proxy header, non-public Host: a LAN login still
    # completes in one step.
    r = client.post("/ui/login", data={"password": "admin-pw"},
                    follow_redirects=False)
    assert r.status_code in (302, 303, 307)


def test_pin_verify_is_throttled(client, monkeypatch):
    # pin_lock_active() is True for a satellite with a PIN set; no app password so
    # the auth middleware is out of the way and we reach the PIN gate directly.
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "kiosk_pin", hash_secret("1234"), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    # A configured satellite (auth delegated off), so the setup-redirect
    # middleware does not bounce /ui/pin/verify to the wizard.
    monkeypatch.setattr(settings, "remote_server_url", "http://server:9284", raising=False)
    monkeypatch.setattr(settings, "upstream_api_key", "k", raising=False)
    for _ in range(rate_limit.pin_guard.max_attempts):
        r = client.post("/ui/pin/verify", data={"pin": "0000"},
                        follow_redirects=False)
        assert r.status_code == 401
    r = client.post("/ui/pin/verify", data={"pin": "0000"}, follow_redirects=False)
    assert r.status_code == 429
