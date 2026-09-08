"""Forgotten-password and email-verification flows through the portal.

Email is stubbed at the transport (email._connect), so a "send" just captures
the EmailMessage. Tests pull the reset/verify token straight out of the
captured link, standing in for the person who clicks it in their inbox."""
import re

import pytest

from app import email as mailer
from app.config import settings
from app.database import SessionLocal
from app.deps import utc_now_iso
from app.models import Account, EmailToken

SIGNUP = {"email": "dan@example.com", "password": "hunter2222",
          "confirm_password": "hunter2222"}


class _FakeSMTP:
    def __init__(self, sent):
        self.sent = sent

    def login(self, *_):
        pass

    def send_message(self, msg):
        self.sent.append(msg)

    def quit(self):
        pass


@pytest.fixture
def email_on(monkeypatch):
    """Turn outgoing email on and capture every message that would be sent."""
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_from", "noreply@forager.test")
    sent: list = []
    monkeypatch.setattr(mailer, "_connect", lambda: _FakeSMTP(sent))
    return sent


def _token_from(msg) -> str:
    link = re.search(r"token=(\S+)", msg.get_content())
    assert link, "no token link in email body"
    return link.group(1)


def _make_account(email="dan@example.com", password_hash="scrypt-placeholder",
                  provider="password", verified=0):
    db = SessionLocal()
    try:
        acc = Account(email=email, password_hash=password_hash,
                      auth_provider=provider, email_verified=verified,
                      created_at=utc_now_iso())
        db.add(acc)
        db.commit()
        return acc.id
    finally:
        db.close()


# --- Forgot-password link visibility ---

def test_forgot_link_hidden_without_email(client):
    assert "Forgot your password" not in client.get("/login").text


def test_forgot_link_shown_with_email(client, email_on):
    assert "Forgot your password" in client.get("/login").text


# --- Forgot flow ---

def test_forgot_unknown_email_gives_same_answer_and_sends_nothing(client, email_on):
    resp = client.post("/forgot", data={"email": "nobody@example.com"})
    assert resp.status_code == 200
    assert "on its way" in resp.text
    assert email_on == []  # no account, so no reset mail


def test_forgot_known_email_mints_token_and_would_send(client, email_on):
    client.post("/signup", data=SIGNUP)
    email_on.clear()  # drop the signup verification email
    resp = client.post("/forgot", data={"email": "dan@example.com"})
    assert resp.status_code == 200
    assert "on its way" in resp.text
    assert len(email_on) == 1
    assert "Reset your Forager password" == email_on[0]["Subject"]

    db = SessionLocal()
    rows = db.query(EmailToken).filter_by(purpose="reset").all()
    db.close()
    assert len(rows) == 1 and rows[0].used == 0


def test_forgot_skips_send_when_email_not_configured(client):
    client.post("/signup", data=SIGNUP)
    # No email_on fixture: email is dark, so nothing is sent and no token is
    # minted, but the response is the same non-committal message.
    resp = client.post("/forgot", data={"email": "dan@example.com"})
    assert resp.status_code == 200
    assert "on its way" in resp.text
    db = SessionLocal()
    assert db.query(EmailToken).count() == 0
    db.close()


def test_forgot_honeypot_sends_nothing(client, email_on):
    client.post("/signup", data=SIGNUP)
    email_on.clear()
    resp = client.post("/forgot", data={"email": "dan@example.com",
                                        "website": "spam"})
    assert resp.status_code == 200
    assert email_on == []


# --- Reset flow ---

def _request_reset(client, email_on, email="dan@example.com"):
    email_on.clear()
    client.post("/forgot", data={"email": email})
    return _token_from(email_on[-1])


def test_reset_valid_token_sets_password_and_kills_sessions(client, email_on):
    resp = client.post("/signup", data=SIGNUP, follow_redirects=False)
    old_cookie = resp.cookies["forager_session"]
    token = _request_reset(client, email_on)

    page = client.get(f"/reset?token={token}")
    assert page.status_code == 200 and "Choose a new password" in page.text

    done = client.post("/reset", data={"token": token,
                                       "new_password": "brandnew1234",
                                       "confirm_password": "brandnew1234"},
                       follow_redirects=False)
    assert done.status_code == 303
    assert done.headers["location"] == "/login?m=password-reset"

    # The reset logged out the session that existed before it.
    client.cookies.clear()
    client.cookies.set("forager_session", old_cookie)
    assert client.get("/account", follow_redirects=False).status_code == 303

    # Old password dead, new one works.
    assert client.post("/v1/accounts/login",
                       json={"email": "dan@example.com",
                             "password": "hunter2222"}).status_code == 401
    assert client.post("/v1/accounts/login",
                       json={"email": "dan@example.com",
                             "password": "brandnew1234"}).status_code == 200


def test_reset_token_is_single_use(client, email_on):
    client.post("/signup", data=SIGNUP)
    token = _request_reset(client, email_on)
    first = client.post("/reset", data={"token": token,
                                        "new_password": "brandnew1234",
                                        "confirm_password": "brandnew1234"},
                        follow_redirects=False)
    assert first.status_code == 303
    again = client.post("/reset", data={"token": token,
                                        "new_password": "another12345",
                                        "confirm_password": "another12345"})
    assert again.status_code == 200
    assert "no longer valid" in again.text


def test_reset_expired_token_rejected(client, email_on):
    client.post("/signup", data=SIGNUP)
    token = _request_reset(client, email_on)
    db = SessionLocal()
    row = db.query(EmailToken).filter_by(purpose="reset").first()
    row.expires_at = "2000-01-01T00:00:00+00:00"
    db.commit()
    db.close()
    resp = client.post("/reset", data={"token": token,
                                       "new_password": "brandnew1234",
                                       "confirm_password": "brandnew1234"})
    assert resp.status_code == 200
    assert "no longer valid" in resp.text


def test_reset_enforces_password_policy(client, email_on):
    client.post("/signup", data=SIGNUP)
    token = _request_reset(client, email_on)
    weak = client.post("/reset", data={"token": token,
                                       "new_password": "short",
                                       "confirm_password": "short"})
    assert weak.status_code == 400
    # The token survives a rejected attempt.
    db = SessionLocal()
    assert db.query(EmailToken).filter_by(purpose="reset").first().used == 0
    db.close()


def test_reset_mismatch_rejected(client, email_on):
    client.post("/signup", data=SIGNUP)
    token = _request_reset(client, email_on)
    resp = client.post("/reset", data={"token": token,
                                       "new_password": "brandnew1234",
                                       "confirm_password": "different1234"})
    assert resp.status_code == 400
    assert "did not match" in resp.text


def test_google_only_account_sets_first_password_via_reset(client, email_on):
    # A password-less Google account resets to set its first password.
    _make_account(email="gina@example.com", password_hash="",
                  provider="google", verified=1)
    token = _request_reset(client, email_on, email="gina@example.com")
    done = client.post("/reset", data={"token": token,
                                       "new_password": "firstpass1234",
                                       "confirm_password": "firstpass1234"},
                       follow_redirects=False)
    assert done.status_code == 303
    assert client.post("/v1/accounts/login",
                       json={"email": "gina@example.com",
                             "password": "firstpass1234"}).status_code == 200


# --- Verification flow ---

def test_signup_leaves_account_unverified_and_still_usable(client, email_on):
    resp = client.post("/signup", data=SIGNUP, follow_redirects=False)
    assert resp.status_code == 303  # signed in immediately
    db = SessionLocal()
    acc = db.query(Account).filter_by(email="dan@example.com").first()
    assert acc.email_verified == 0
    db.close()
    # A verification email went out, and the banner nudges them.
    assert any(m["Subject"] == "Confirm your email for Forager" for m in email_on)
    assert "confirm your email" in client.get("/account").text.lower()


def test_verify_link_marks_verified(client, email_on):
    client.post("/signup", data=SIGNUP)
    token = _token_from(email_on[-1])
    page = client.get(f"/verify?token={token}")
    assert page.status_code == 200 and "confirmed" in page.text.lower()
    db = SessionLocal()
    acc = db.query(Account).filter_by(email="dan@example.com").first()
    assert acc.email_verified == 1
    db.close()
    # Banner is gone now.
    assert "confirm your email" not in client.get("/account").text.lower()


def test_verify_bad_token_is_friendly(client, email_on):
    client.post("/signup", data=SIGNUP)
    resp = client.get("/verify?token=prv_nonsense")
    assert resp.status_code == 200
    assert "not valid" in resp.text.lower()


def test_resend_verification(client, email_on):
    client.post("/signup", data=SIGNUP)
    email_on.clear()
    resp = client.post("/account/verify/resend", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/account?m=verification-sent"
    assert any(m["Subject"] == "Confirm your email for Forager" for m in email_on)


def test_no_verification_banner_or_send_when_email_off(client):
    client.post("/signup", data=SIGNUP)
    db = SessionLocal()
    assert db.query(EmailToken).count() == 0
    db.close()
    assert "confirm your email" not in client.get("/account").text.lower()
