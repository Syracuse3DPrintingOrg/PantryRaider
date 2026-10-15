"""The instance verify-login contract.

An instance calls POST /v1/instance/verify-login to confirm that a Forager
email + password (and 2FA, if the account has it) belong to the SAME account
the instance is linked to. This backs the app's "sign in with Forager" login
path. The tests drive the real route with a TestClient and enable 2FA straight
in the database the way the portal enrollment would.
"""
import os


os.environ.setdefault("CLOUD_DATABASE_URL", "sqlite://")

from app.database import SessionLocal
from app.models import Account
from app.routers.accounts import replace_recovery_codes
from app.security import generate_totp_secret, totp_now

CREDS = {"email": "dan@example.com", "password": "hunter2222"}


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def _enable_2fa(email="dan@example.com"):
    """Turn on the account's 2FA directly and return (secret, recovery_codes)."""
    secret = generate_totp_secret()
    db = SessionLocal()
    try:
        account = db.query(Account).filter_by(email=email).first()
        account.totp_secret = secret
        account.totp_enabled = 1
        db.commit()
        codes = replace_recovery_codes(db, account.id)
    finally:
        db.close()
    return secret, codes


def test_verify_login_matches_linked_account(client, instance_token):
    r = client.post("/v1/instance/verify-login", headers=_headers(instance_token),
                    json=CREDS)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["account_email"] == "dan@example.com"


def test_verify_login_rejects_wrong_password(client, instance_token):
    r = client.post("/v1/instance/verify-login", headers=_headers(instance_token),
                    json={**CREDS, "password": "not-the-password"})
    assert r.status_code == 401
    assert r.json() == {"error": "invalid_credentials"}


def test_verify_login_rejects_a_different_account(client, instance_token):
    # A second, valid account whose credentials are correct but is NOT the
    # account this instance is linked to: refused exactly like a wrong password.
    other = client.post("/v1/accounts/signup",
                        json={"email": "eve@example.com", "password": "hunter3333"})
    assert other.status_code == 200
    r = client.post("/v1/instance/verify-login", headers=_headers(instance_token),
                    json={"email": "eve@example.com", "password": "hunter3333"})
    assert r.status_code == 401
    assert r.json() == {"error": "invalid_credentials"}


def test_verify_login_bad_instance_token(client):
    r = client.post("/v1/instance/verify-login",
                    headers=_headers("prc_not_a_real_token"), json=CREDS)
    assert r.status_code == 401


def test_verify_login_honours_account_2fa(client, instance_token):
    secret, codes = _enable_2fa()

    # Missing code once 2FA is on: a machine-readable prompt to collect one.
    missing = client.post("/v1/instance/verify-login",
                         headers=_headers(instance_token), json=CREDS)
    assert missing.status_code == 401
    assert missing.json() == {"error": "totp_required"}

    # Wrong code: distinct so the app can say it was wrong.
    wrong = client.post("/v1/instance/verify-login",
                       headers=_headers(instance_token),
                       json={**CREDS, "totp": "000000"})
    assert wrong.status_code == 401
    assert wrong.json() == {"error": "totp_invalid"}

    # Correct code passes.
    good = client.post("/v1/instance/verify-login",
                      headers=_headers(instance_token),
                      json={**CREDS, "totp": totp_now(secret)})
    assert good.status_code == 200
    assert good.json()["ok"] is True

    # A recovery code works too, and burns.
    rec = client.post("/v1/instance/verify-login",
                     headers=_headers(instance_token),
                     json={**CREDS, "totp": codes[0]})
    assert rec.status_code == 200
    reuse = client.post("/v1/instance/verify-login",
                       headers=_headers(instance_token),
                       json={**CREDS, "totp": codes[0]})
    assert reuse.json() == {"error": "totp_invalid"}


def test_instance_me_reports_account_2fa(client, instance_token):
    off = client.get("/v1/instance/me", headers=_headers(instance_token))
    assert off.status_code == 200
    assert off.json()["account_2fa"] is False

    _enable_2fa()
    on = client.get("/v1/instance/me", headers=_headers(instance_token))
    assert on.json()["account_2fa"] is True


def test_verify_login_rate_limited_per_instance(client, instance_token, monkeypatch):
    from app import ratelimit
    ratelimit.reset()
    monkeypatch.setattr("app.config.settings.login_rate_per_minute", 3)
    seen_429 = False
    for _ in range(6):
        r = client.post("/v1/instance/verify-login",
                       headers=_headers(instance_token),
                       json={**CREDS, "password": "wrong"})
        if r.status_code == 429:
            seen_429 = True
    assert seen_429
