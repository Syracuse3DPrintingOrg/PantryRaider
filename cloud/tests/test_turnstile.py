"""Turnstile signup CAPTCHA: gating, verification, and fail-open policy."""
import httpx
import pytest

from app import turnstile
from app.config import settings


@pytest.fixture(autouse=True)
def _reset():
    old = (settings.turnstile_site_key, settings.turnstile_secret, turnstile.transport)
    yield
    (settings.turnstile_site_key, settings.turnstile_secret, turnstile.transport) = old


def _mock(success):
    def handler(req):
        return httpx.Response(200, json={"success": success})
    return httpx.MockTransport(handler)


def test_dark_when_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "turnstile_site_key", "", raising=False)
    monkeypatch.setattr(settings, "turnstile_secret", "", raising=False)
    assert turnstile.enabled() is False
    assert turnstile.verify("") is True   # off: never blocks


def test_enabled_needs_both_keys(monkeypatch):
    monkeypatch.setattr(settings, "turnstile_site_key", "site", raising=False)
    monkeypatch.setattr(settings, "turnstile_secret", "", raising=False)
    assert turnstile.enabled() is False


def test_verify_success_and_failure(monkeypatch):
    monkeypatch.setattr(settings, "turnstile_site_key", "site", raising=False)
    monkeypatch.setattr(settings, "turnstile_secret", "sec", raising=False)
    monkeypatch.setattr(turnstile, "transport", _mock(True))
    assert turnstile.verify("tok") is True
    monkeypatch.setattr(turnstile, "transport", _mock(False))
    assert turnstile.verify("tok") is False
    # No token at all is a fail.
    assert turnstile.verify("") is False


def test_fail_open_when_cloudflare_unreachable(monkeypatch):
    monkeypatch.setattr(settings, "turnstile_site_key", "site", raising=False)
    monkeypatch.setattr(settings, "turnstile_secret", "sec", raising=False)
    def boom(req):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(turnstile, "transport", httpx.MockTransport(boom))
    # A Cloudflare outage does not block real signups (other layers still apply).
    assert turnstile.verify("tok") is True


def test_fail_closed_blocks_when_cloudflare_unreachable(monkeypatch):
    monkeypatch.setattr(settings, "turnstile_site_key", "site", raising=False)
    monkeypatch.setattr(settings, "turnstile_secret", "sec", raising=False)
    def boom(req):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(turnstile, "transport", httpx.MockTransport(boom))
    # fail_open=False (the signup policy): an unverifiable challenge blocks.
    assert turnstile.verify("tok", fail_open=False) is False


def test_signup_blocked_when_challenge_cannot_be_verified(client, monkeypatch):
    from app import turnstile as ts
    monkeypatch.setattr(settings, "turnstile_site_key", "site", raising=False)
    monkeypatch.setattr(settings, "turnstile_secret", "sec", raising=False)
    def boom(req):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(ts, "transport", httpx.MockTransport(boom))
    # Signup fails closed: Cloudflare unreachable must not wave a new account
    # through when the human-check cannot be confirmed.
    r = client.post("/signup", data={"email": "new@example.com",
                                     "password": "k7-mango-lantern",
                                     "confirm_password": "k7-mango-lantern",
                                     "cf-turnstile-response": "tok"})
    assert r.status_code == 400 and "challenge" in r.text.lower()
    from app.database import SessionLocal
    from app.models import Account
    db = SessionLocal()
    try:
        assert db.query(Account).filter_by(email="new@example.com").first() is None
    finally:
        db.close()


def test_signup_form_shows_widget_when_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "turnstile_site_key", "0xSITE", raising=False)
    monkeypatch.setattr(settings, "turnstile_secret", "0xSEC", raising=False)
    page = client.get("/signup").text
    assert "cf-turnstile" in page and "0xSITE" in page
    assert "challenges.cloudflare.com" in page


def test_signup_blocked_when_challenge_fails(client, monkeypatch):
    monkeypatch.setattr(settings, "turnstile_site_key", "site", raising=False)
    monkeypatch.setattr(settings, "turnstile_secret", "sec", raising=False)
    monkeypatch.setattr(turnstile, "transport", _mock(False))
    r = client.post("/signup", data={"email": "new@example.com",
                                     "password": "k7-mango-lantern",
                                     "confirm_password": "k7-mango-lantern",
                                     "cf-turnstile-response": "bad"})
    assert r.status_code == 400 and "challenge" in r.text.lower()
