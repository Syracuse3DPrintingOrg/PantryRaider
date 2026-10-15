"""Sign-in pages refuse cross-origin posts, reset mail is capped per account,
a password change spends every older reset link, and a single-use code can
only be spent once."""
import pytest

from app.config import settings
from app.database import SessionLocal
from app.deps import claim_once
from app.models import EmailToken, Instance, PairingCode
from tests.test_email_flows import SIGNUP, _request_reset, email_on  # noqa: F401

KITCHEN = {"Origin": "https://evil.forager.pantryraider.app"}


@pytest.mark.parametrize("path,data", [
    ("/login", {"email": "dan@example.com", "password": "hunter2222"}),
    ("/signup", SIGNUP),
    ("/login/2fa", {"code": "123456"}),
    ("/forgot", {"email": "dan@example.com"}),
    ("/reset", {"token": "x", "new_password": "brandnew1234",
                "confirm_password": "brandnew1234"}),
])
def test_sign_in_pages_refuse_a_kitchen_origin(client, path, data):
    # A kitchen page is same-site with the portal, so without this it could
    # sign a visitor into an account the page's owner controls.
    resp = client.post(path, data=data, headers=KITCHEN, follow_redirects=False)
    assert resp.status_code == 403


def test_portal_login_still_works_from_the_portal(client):
    client.post("/signup", data=SIGNUP)
    client.cookies.clear()
    resp = client.post("/login", data={"email": "dan@example.com",
                                       "password": "hunter2222"},
                       headers={"Origin": "http://testserver"},
                       follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/account"


def test_reset_mail_is_capped_per_account(client, email_on, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "forgot_per_account_per_hour", 2)
    client.post("/signup", data=SIGNUP)
    email_on.clear()
    for _ in range(4):
        resp = client.post("/forgot", data={"email": "dan@example.com"})
        # Same answer every time, so the cap reveals nothing.
        assert resp.status_code == 200 and "on its way" in resp.text
    assert len(email_on) == 2


def test_password_change_spends_older_reset_links(client, email_on):  # noqa: F811
    client.post("/signup", data=SIGNUP)
    token = _request_reset(client, email_on)
    changed = client.post("/account/password",
                          data={"current_password": "hunter2222",
                                "new_password": "brandnew1234",
                                "confirm_password": "brandnew1234"},
                          follow_redirects=False)
    assert changed.headers["location"] == "/account?m=password-changed"
    page = client.get(f"/reset?token={token}")
    assert "no longer valid" in page.text


def test_a_reset_spends_the_other_reset_links(client, email_on):  # noqa: F811
    client.post("/signup", data=SIGNUP)
    first = _request_reset(client, email_on)
    second = _request_reset(client, email_on)
    done = client.post("/reset", data={"token": second,
                                       "new_password": "brandnew1234",
                                       "confirm_password": "brandnew1234"},
                       follow_redirects=False)
    assert done.status_code == 303
    db = SessionLocal()
    try:
        assert db.query(EmailToken).filter_by(purpose="reset", used=0).count() == 0
    finally:
        db.close()
    assert "no longer valid" in client.get(f"/reset?token={first}").text


def test_a_pairing_code_is_spent_only_once(client, session_token):
    code = client.post("/v1/pairing/code",
                       headers={"Authorization": f"Bearer {session_token}"}
                       ).json()["code"]
    db = SessionLocal()
    try:
        row = db.query(PairingCode).first()
        # Two requests that both read the code as unspent: only the first
        # conditional update wins.
        assert claim_once(db, PairingCode, row.id, "redeemed") is True
        assert claim_once(db, PairingCode, row.id, "redeemed") is False
        db.rollback()
    finally:
        db.close()
    assert client.post("/v1/pairing/redeem", json={"code": code}).status_code == 200
    assert client.post("/v1/pairing/redeem", json={"code": code}).status_code == 400
    db = SessionLocal()
    try:
        assert db.query(Instance).count() == 1
    finally:
        db.close()
