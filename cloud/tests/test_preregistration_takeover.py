"""Pre-registration takeover (FoodAssistant-fa4).

Anyone can sign up with someone else's email address. When the real owner
later proves the address, by Google sign-in or by a password reset link from
their inbox, whatever the earlier holder set up must stop working."""
import re
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app import email as mailer
from app.config import settings
from app.database import SessionLocal
from app.models import Account, Instance
from app.routers import oauth_google

VICTIM = "victim@gmail.com"
ATTACKER_PASSWORD = "attacker-knows-1"


class _FakeSMTP:
    def __init__(self, sent):
        self.sent = sent

    def login(self, *_):
        pass

    def send_message(self, msg):
        self.sent.append(msg)

    def quit(self):
        pass


def _account():
    db = SessionLocal()
    try:
        return db.query(Account).filter_by(email=VICTIM).first()
    finally:
        db.close()


def _preregister(client):
    """The attacker signs up with the victim's address and pairs a kitchen."""
    r = client.post("/v1/accounts/signup", json={
        "email": VICTIM, "password": ATTACKER_PASSWORD,
        "install_key": "attacker-install"})
    assert r.status_code == 200
    session = r.json()["session_token"]
    p = client.post("/v1/instances/provision",
                    json={"email": VICTIM, "password": ATTACKER_PASSWORD})
    assert p.status_code == 200
    return session, p.json()["instance_token"]


def _attacker_locked_out(client, session, instance_token):
    client.cookies.clear()
    assert client.post("/v1/accounts/login", json={
        "email": VICTIM, "password": ATTACKER_PASSWORD}).status_code == 401
    assert client.get("/v1/instance/me", headers={
        "Authorization": f"Bearer {instance_token}"}).status_code == 401
    assert client.get("/v1/accounts/me", headers={
        "Authorization": f"Bearer {session}"}).status_code == 401


@pytest.fixture
def google_as_victim(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "cid")
    monkeypatch.setattr(settings, "google_client_secret", "cs")

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at"})
        return httpx.Response(200, json={"email": VICTIM, "email_verified": True})
    monkeypatch.setattr(oauth_google, "transport", httpx.MockTransport(handler))


def test_google_signin_evicts_the_unverified_holder(client, google_as_victim):
    session, instance_token = _preregister(client)
    start = client.get("/auth/google/start?intent=signup", follow_redirects=False)
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    cb = client.get(f"/auth/google/callback?code=c&state={state}",
                    follow_redirects=False)
    assert cb.status_code == 303 and cb.headers["location"] == "/account"
    acct = _account()
    assert acct.email_verified == 1
    assert acct.password_hash == ""
    db = SessionLocal()
    try:
        assert db.query(Instance).filter_by(account_id=acct.id).count() == 0
    finally:
        db.close()
    _attacker_locked_out(client, session, instance_token)


def test_google_signin_leaves_a_verified_account_alone(client, google_as_victim):
    session, instance_token = _preregister(client)
    db = SessionLocal()
    try:
        db.query(Account).filter_by(email=VICTIM).first().email_verified = 1
        db.commit()
    finally:
        db.close()
    start = client.get("/auth/google/start?intent=signin", follow_redirects=False)
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    client.get(f"/auth/google/callback?code=c&state={state}",
               follow_redirects=False)
    client.cookies.clear()
    assert client.post("/v1/accounts/login", json={
        "email": VICTIM, "password": ATTACKER_PASSWORD}).status_code == 200
    assert client.get("/v1/instance/me", headers={
        "Authorization": f"Bearer {instance_token}"}).status_code == 200


def test_password_reset_evicts_the_unverified_holder(client, monkeypatch):
    session, instance_token = _preregister(client)
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_from", "noreply@forager.test")
    sent: list = []
    monkeypatch.setattr(mailer, "_connect", lambda: _FakeSMTP(sent))
    client.cookies.clear()
    client.post("/forgot", data={"email": VICTIM})
    token = re.search(r"token=(\S+)", sent[-1].get_content()).group(1)
    r = client.post("/reset", data={"token": token,
                                    "new_password": "owner-password-9",
                                    "confirm_password": "owner-password-9"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert _account().email_verified == 1
    _attacker_locked_out(client, session, instance_token)
    assert client.post("/v1/accounts/login", json={
        "email": VICTIM, "password": "owner-password-9"}).status_code == 200


def test_password_reset_signs_out_every_session(client, monkeypatch):
    r = client.post("/v1/accounts/signup", json={
        "email": VICTIM, "password": ATTACKER_PASSWORD, "install_key": "k"})
    session = r.json()["session_token"]
    db = SessionLocal()
    try:
        db.query(Account).filter_by(email=VICTIM).first().email_verified = 1
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_from", "noreply@forager.test")
    sent: list = []
    monkeypatch.setattr(mailer, "_connect", lambda: _FakeSMTP(sent))
    client.post("/forgot", data={"email": VICTIM})
    token = re.search(r"token=(\S+)", sent[-1].get_content()).group(1)
    client.post("/reset", data={"token": token,
                                "new_password": "owner-password-9",
                                "confirm_password": "owner-password-9"})
    assert client.get("/v1/accounts/me", headers={
        "Authorization": f"Bearer {session}"}).status_code == 401
