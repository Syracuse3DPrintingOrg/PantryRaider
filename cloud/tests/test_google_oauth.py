"""Google sign-in: portal flow, app-return flow, gating, and state checks."""
import re
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.routers import oauth_google
from app.security import totp_now


@pytest.fixture
def google(monkeypatch):
    """Enable the feature and stand in for Google's two endpoints."""
    monkeypatch.setattr(settings, "google_client_id", "test-client-id")
    monkeypatch.setattr(settings, "google_client_secret", "test-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "google-at"})
        if request.url.host == "openidconnect.googleapis.com":
            assert request.headers["Authorization"] == "Bearer google-at"
            return httpx.Response(200, json={"email": "Gina@Example.com",
                                             "email_verified": True})
        return httpx.Response(404)

    monkeypatch.setattr(oauth_google, "transport", httpx.MockTransport(handler))


def start_and_callback(client, path="/auth/google/start"):
    """Drive the browser round-trip: start, then come back with the code."""
    start = client.get(path, follow_redirects=False)
    assert start.status_code == 303
    target = urlsplit(start.headers["location"])
    assert target.netloc == "accounts.google.com"
    state = parse_qs(target.query)["state"][0]
    return client.get(f"/auth/google/callback?code=fake-code&state={state}",
                      follow_redirects=False)


def test_gated_off_by_default(client):
    assert "Continue with Google" not in client.get("/login").text
    assert "Continue with Google" not in client.get("/signup").text
    assert client.get("/auth/google/start",
                      follow_redirects=False).status_code == 404
    assert client.get("/v1/meta").json() == {"oauth_google": False}


def test_enabled_rendering_and_meta(client, google):
    assert "Continue with Google" in client.get("/login").text
    assert "Continue with Google" in client.get("/signup").text
    assert client.get("/v1/meta").json() == {"oauth_google": True}


def test_new_account_via_google(client, google):
    resp = start_and_callback(client, "/auth/google/start?intent=signup")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/account"

    page = client.get("/account").text
    assert "gina@example.com" in page  # normalised like password signup
    assert "Set a password" in page  # no password yet, the page offers one

    from app.database import SessionLocal
    from app.models import Account
    db = SessionLocal()
    account = db.query(Account).filter_by(email="gina@example.com").one()
    db.close()
    assert account.auth_provider == "google"
    assert account.password_hash == ""

    # No password means no password login until they set one.
    denied = client.post("/v1/accounts/login",
                         json={"email": "gina@example.com", "password": ""})
    assert denied.status_code == 401


def test_google_account_can_set_a_password(client, google):
    start_and_callback(client, "/auth/google/start?intent=signup")
    resp = client.post("/account/password",
                       data={"new_password": "newpass9999",
                             "confirm_password": "newpass9999"},
                       follow_redirects=False)
    assert resp.headers["location"] == "/account?m=password-set"
    assert client.post("/v1/accounts/login",
                       json={"email": "gina@example.com",
                             "password": "newpass9999"}).status_code == 200
    assert "Change password" in client.get("/account").text


def test_existing_email_signs_in_to_its_account(client, google):
    client.post("/v1/accounts/signup",
                json={"email": "gina@example.com", "password": "hunter2222"})
    resp = start_and_callback(client)
    assert resp.headers["location"] == "/account"

    from app.database import SessionLocal
    from app.models import Account
    db = SessionLocal()
    accounts = db.query(Account).filter_by(email="gina@example.com").all()
    db.close()
    assert len(accounts) == 1  # linked, not duplicated
    assert accounts[0].password_hash != ""  # the password stays usable


def test_state_mismatch_is_rejected(client, google):
    client.get("/auth/google/start", follow_redirects=False)
    resp = client.get("/auth/google/callback?code=fake-code&state=forged",
                      follow_redirects=False)
    assert resp.status_code == 400
    # No cookie at all is rejected the same way.
    client.cookies.clear()
    resp = client.get("/auth/google/callback?code=fake-code&state=x",
                      follow_redirects=False)
    assert resp.status_code == 400


def test_unverified_email_is_rejected(client, google, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "google-at"})
        return httpx.Response(200, json={"email": "gina@example.com",
                                         "email_verified": False})

    monkeypatch.setattr(oauth_google, "transport", httpx.MockTransport(handler))
    assert start_and_callback(client).status_code == 403


def test_app_return_flow_mints_a_redeemable_code(client, google):
    resp = start_and_callback(
        client,
        "/auth/google/start?flow=app&device_name=Kitchen%20Pi"
        "&return_url=http://127.0.0.1:9284/cloud/oauth-return")
    assert resp.status_code == 303
    target = urlsplit(resp.headers["location"])
    assert target.scheme == "http" and target.netloc == "127.0.0.1:9284"
    assert target.path == "/cloud/oauth-return"
    code = parse_qs(target.query)["code"][0]

    redeemed = client.post("/v1/pairing/redeem",
                           json={"code": code, "name": "Kitchen Pi"})
    assert redeemed.status_code == 200
    token = redeemed.json()["instance_token"]
    me = client.get("/v1/instance/me",
                    headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["account_email"] == "gina@example.com"

    # Single use: the code died on redemption.
    again = client.post("/v1/pairing/redeem",
                        json={"code": code, "name": "Kitchen Pi"})
    assert again.status_code == 400


def _enroll_2fa(client):
    """Turn 2FA on for the signed-in portal client; return the TOTP secret."""
    page = client.get("/account/2fa/setup")
    secret = re.search(r'name="secret" value="([A-Z0-9]+)"', page.text).group(1)
    resp = client.post("/account/2fa/enable",
                       data={"secret": secret, "code": totp_now(secret)})
    assert resp.status_code == 200
    return secret


def test_app_return_2fa_challenges_before_minting_a_code(client, google):
    # Make a Google account and turn 2FA on (the callback left a portal session).
    assert start_and_callback(
        client, "/auth/google/start?intent=signup").headers["location"] == "/account"
    secret = _enroll_2fa(client)

    # A fresh app-return Google login must NOT hand out a provision code: Google
    # proved the email but not the second factor, so it lands on the challenge.
    browser = TestClient(app)
    ret = "http://127.0.0.1:9284/cloud/oauth-return"
    resp = start_and_callback(
        browser,
        f"/auth/google/start?flow=app&device_name=Kitchen%20Pi&return_url={ret}")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login/2fa"
    assert "code=" not in resp.headers["location"]  # nothing leaked

    # A correct code sends the browser back to the app with a redeemable code.
    done = browser.post("/login/2fa", data={"code": totp_now(secret)},
                        follow_redirects=False)
    assert done.status_code == 303
    target = urlsplit(done.headers["location"])
    assert target.scheme == "http" and target.netloc == "127.0.0.1:9284"
    assert target.path == "/cloud/oauth-return"
    code = parse_qs(target.query)["code"][0]

    redeemed = client.post("/v1/pairing/redeem",
                           json={"code": code, "name": "Kitchen Pi"})
    assert redeemed.status_code == 200
    me = client.get("/v1/instance/me",
                    headers={"Authorization":
                             f"Bearer {redeemed.json()['instance_token']}"})
    assert me.json()["account_email"] == "gina@example.com"


def test_app_return_without_2fa_mints_immediately(client, google):
    # The no-2FA app-return path is unchanged: a provision code straight away,
    # no challenge (also covered by test_app_return_flow_mints_a_redeemable_code).
    resp = start_and_callback(
        client,
        "/auth/google/start?flow=app"
        "&return_url=http://127.0.0.1:9284/cloud/oauth-return")
    assert resp.status_code == 303
    target = urlsplit(resp.headers["location"])
    assert target.path == "/cloud/oauth-return"
    assert "code" in parse_qs(target.query)


def test_app_flow_rejects_bad_return_urls(client, google):
    for bad in ("javascript:alert(1)", "ftp://x/y", "not-a-url", ""):
        resp = client.get("/auth/google/start",
                          params={"flow": "app", "return_url": bad},
                          follow_redirects=False)
        assert resp.status_code == 400


def test_signin_with_no_account_does_not_create(client, google):
    # Default intent is signin: a Google login for an unknown email must not
    # create an account, and should send the user to sign up.
    resp = start_and_callback(client, "/auth/google/start?intent=signin")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?m=google-no-account"
    from app.database import SessionLocal
    from app.models import Account
    db = SessionLocal()
    assert db.query(Account).filter_by(email="gina@example.com").first() is None
    db.close()


def test_google_signin_verifies_a_preexisting_account(client, google):
    # A password account made before email was configured is unverified; signing
    # in with Google (which proved the address) marks it verified.
    from app.database import SessionLocal
    from app.models import Account
    from app.security import hash_password
    db = SessionLocal()
    db.add(Account(email="gina@example.com", password_hash=hash_password("k7-mango-lantern"),
                   auth_provider="password", email_verified=0,
                   created_at="2026-01-01T00:00:00+00:00"))
    db.commit(); db.close()
    resp = start_and_callback(client, "/auth/google/start?intent=signin")
    assert resp.status_code == 303 and resp.headers["location"] == "/account"
    db = SessionLocal()
    acct = db.query(Account).filter_by(email="gina@example.com").first()
    assert acct.email_verified == 1
    db.close()
