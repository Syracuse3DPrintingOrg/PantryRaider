"""Adding a passkey needs more than a session cookie, the owner hears about
it, and a sign-in without user verification still meets two-factor.

The authenticator is stubbed through the webauthn_service verify seam, as in
test_passkeys.py."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import webauthn_service as wa
from app.database import SessionLocal
from app.main import app
from app.models import Account, AuthSession, WebAuthnCredential
from app.routers import passkeys
from app.routers.accounts import _issue_session
from app.security import generate_totp_secret, totp_now
from app.totp_crypto import encrypt_secret

EMAIL = "dan@example.com"
PASSWORD = "hunter2222"


class _Reg:
    def __init__(self, cred_id=b"new-cred", user_verified=True):
        self.credential_id = cred_id
        self.credential_public_key = b"pubkey-bytes"
        self.sign_count = 1
        self.user_verified = user_verified


class _Auth:
    def __init__(self, user_verified):
        self.new_sign_count = 2
        self.user_verified = user_verified


def _password_client():
    client = TestClient(app)
    resp = client.post("/signup", data={"email": EMAIL, "password": PASSWORD,
                                        "confirm_password": PASSWORD},
                       follow_redirects=False)
    assert resp.status_code == 303
    return client


def _account():
    db = SessionLocal()
    try:
        return db.query(Account).filter_by(email=EMAIL).first()
    finally:
        db.close()


def _google_client(minutes_ago=0, email=EMAIL):
    """A browser signed in to an account with no password (made with Google),
    its session opened ``minutes_ago``."""
    db = SessionLocal()
    try:
        acct = Account(email=email, password_hash="", auth_provider="google",
                       email_verified=1, created_at="2026-01-01T00:00:00+00:00")
        db.add(acct)
        db.commit()
        token = _issue_session(db, acct.id)
        if minutes_ago:
            opened = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
            sess = db.query(AuthSession).filter_by(account_id=acct.id).one()
            sess.created_at = opened.isoformat(timespec="seconds")
            db.commit()
    finally:
        db.close()
    client = TestClient(app)
    client.cookies.set("forager_session", token)
    return client


def _turn_on_totp():
    """Turn two-factor on for the account and return its secret."""
    secret = generate_totp_secret()
    db = SessionLocal()
    try:
        acct = db.query(Account).filter_by(email=EMAIL).one()
        acct.totp_secret = encrypt_secret(secret)
        acct.totp_enabled = 1
        db.commit()
    finally:
        db.close()
    return secret


def _begin(client, **proof):
    return client.post("/account/passkeys/register/begin", json=proof)


# --- Step-up before adding a passkey ---------------------------------------

def test_cookie_alone_cannot_start_adding_a_passkey():
    client = _password_client()
    resp = _begin(client)
    assert resp.status_code == 403
    assert resp.json()["step_up"] == ["password"]
    assert "password" in resp.json()["error"]
    assert "forager_passkey_reg" not in resp.cookies


def test_wrong_password_is_refused_and_counted():
    client = _password_client()
    resp = _begin(client, password="not-the-password")
    assert resp.status_code == 403
    assert resp.json()["error"] == "That password did not match."
    # Counted toward the same lockout the login page uses.
    assert _account().failed_logins == 1


def test_password_unlocks_adding_a_passkey():
    client = _password_client()
    resp = _begin(client, password=PASSWORD)
    assert resp.status_code == 200
    assert "forager_passkey_reg" in resp.cookies


def test_finish_without_a_stepped_up_begin_adds_nothing(monkeypatch):
    monkeypatch.setattr(wa, "verify_registration", lambda **kw: _Reg())
    client = _password_client()
    _begin(client)  # refused, so no challenge is stashed
    resp = client.post("/account/passkeys/register/finish",
                       json={"credential": {"id": "x", "response": {}}})
    assert resp.status_code == 400
    db = SessionLocal()
    try:
        assert db.query(WebAuthnCredential).count() == 0
    finally:
        db.close()


def test_authenticator_code_unlocks_adding_a_passkey():
    client = _password_client()
    secret = _turn_on_totp()
    refused = _begin(client)
    assert refused.status_code == 403
    assert refused.json()["step_up"] == ["password", "code"]
    assert _begin(client, code=totp_now(secret)).status_code == 200


def test_wrong_code_is_refused_and_counted():
    client = _password_client()
    _turn_on_totp()
    resp = _begin(client, code="000000")
    assert resp.status_code == 403
    assert resp.json()["error"] == "That code did not match."
    assert _account().totp_failures == 1


def test_account_without_password_may_add_right_after_signing_in():
    client = _google_client(minutes_ago=0)
    assert _begin(client).status_code == 200


def test_account_without_password_must_sign_in_again_later():
    client = _google_client(minutes_ago=passkeys.RECENT_SIGNIN_MINUTES + 5)
    resp = _begin(client)
    assert resp.status_code == 403
    assert resp.json()["step_up"] == ["reauth"]
    assert "sign out and sign back in" in resp.json()["error"]
    # A password typed at an account that has none proves nothing.
    assert _begin(client, password="anything").status_code == 403


def test_account_without_password_can_use_its_code_later():
    client = _google_client(minutes_ago=passkeys.RECENT_SIGNIN_MINUTES + 5)
    secret = _turn_on_totp()
    refused = _begin(client)
    assert refused.json()["step_up"] == ["code"]
    assert _begin(client, code=totp_now(secret)).status_code == 200


def test_security_pane_asks_for_the_proof():
    page = _password_client().get("/account").text
    assert 'id="passkey-password"' in page
    google_page = _google_client(email="gina@example.com").get("/account").text
    assert 'id="passkey-password"' not in google_page
    assert "sign out and sign back in first" in google_page


# --- The owner is told, and user verification is recorded -----------------

def test_adding_a_passkey_emails_the_owner(monkeypatch):
    sent = []
    monkeypatch.setattr(passkeys, "send_email",
                        lambda to, subject, body: sent.append((to, subject,
                                                               body)) or True)
    monkeypatch.setattr(wa, "verify_registration", lambda **kw: _Reg())
    client = _password_client()
    _begin(client, password=PASSWORD)
    resp = client.post("/account/passkeys/register/finish",
                       json={"nickname": "Work laptop",
                             "credential": {"id": "x", "response": {}}})
    assert resp.status_code == 200
    assert len(sent) == 1
    to, subject, body = sent[0]
    assert to == EMAIL
    assert "passkey" in subject.lower()
    assert "Work laptop" in body and "/account#security" in body


def test_adding_a_passkey_works_without_email_set_up(monkeypatch):
    # No SMTP host in the test settings: the add still succeeds quietly.
    monkeypatch.setattr(wa, "verify_registration", lambda **kw: _Reg())
    client = _password_client()
    _begin(client, password=PASSWORD)
    resp = client.post("/account/passkeys/register/finish",
                       json={"credential": {"id": "x", "response": {}}})
    assert resp.status_code == 200 and resp.json()["ok"] is True


def test_registration_records_user_verification(monkeypatch):
    client = _password_client()
    for cred_id, uv in ((b"uv-key", True), (b"plain-key", False)):
        monkeypatch.setattr(wa, "verify_registration",
                            lambda **kw: _Reg(cred_id=cred_id,
                                              user_verified=uv))
        _begin(client, password=PASSWORD)
        assert client.post("/account/passkeys/register/finish",
                           json={"credential": {"id": "x", "response": {}}}
                           ).status_code == 200
    db = SessionLocal()
    try:
        flags = {c.credential_id: c.user_verified
                 for c in db.query(WebAuthnCredential).all()}
    finally:
        db.close()
    assert flags == {wa.bytes_to_base64url(b"uv-key"): 1,
                     wa.bytes_to_base64url(b"plain-key"): 0}


# --- Signing in: user verification decides whether two-factor still asks ---

def _sign_in_with_passkey(monkeypatch, user_verified):
    acct = _account()
    cred_id = wa.bytes_to_base64url(b"login-cred")
    db = SessionLocal()
    try:
        # A passkey added before user verification was recorded (null flag).
        db.add(WebAuthnCredential(
            account_id=acct.id, credential_id=cred_id,
            public_key=wa.bytes_to_base64url(b"pubkey-bytes"), sign_count=1,
            transports="usb", nickname="Key",
            created_at="2026-07-07T00:00:00+00:00"))
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(wa, "verify_authentication",
                        lambda **kw: _Auth(user_verified))
    browser = TestClient(app)
    browser.post("/login/passkey/begin", json={})
    resp = browser.post("/login/passkey/finish",
                        json={"credential": {"id": cred_id, "rawId": cred_id,
                                             "type": "public-key",
                                             "response": {}}})
    return browser, resp


def test_verified_passkey_skips_the_code_step(monkeypatch):
    _password_client()
    _turn_on_totp()
    browser, resp = _sign_in_with_passkey(monkeypatch, user_verified=True)
    assert resp.status_code == 200
    assert resp.json()["next"] == "/account"
    assert "forager_session" in resp.cookies


def test_unverified_passkey_goes_on_to_the_code_step(monkeypatch):
    _password_client()
    secret = _turn_on_totp()
    browser, resp = _sign_in_with_passkey(monkeypatch, user_verified=False)
    assert resp.status_code == 200
    assert resp.json()["next"] == "/login/2fa"
    assert "forager_session" not in resp.cookies
    assert "forager_totp_pending" in resp.cookies
    # Not signed in until the code is entered.
    assert browser.get("/account", follow_redirects=False
                       ).headers["location"] == "/login"
    assert browser.get("/login/2fa").status_code == 200
    done = browser.post("/login/2fa", data={"code": totp_now(secret)},
                        follow_redirects=False)
    assert done.headers["location"] == "/account"
    assert EMAIL in browser.get("/account").text


def test_unverified_passkey_signs_in_when_two_factor_is_off(monkeypatch):
    _password_client()
    browser, resp = _sign_in_with_passkey(monkeypatch, user_verified=False)
    assert resp.status_code == 200
    assert resp.json()["next"] == "/account"
    assert "forager_session" in resp.cookies


def test_security_pane_lists_passkey_dates(monkeypatch):
    client = _password_client()
    _sign_in_with_passkey(monkeypatch, user_verified=True)
    page = client.get("/account").text
    assert "Added 2026-07-07" in page
    today = datetime.now(timezone.utc).date().isoformat()
    assert f"last used {today}" in page
