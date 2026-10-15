"""Trial farming and unmetered AI spend (FoodAssistant-fa2).

Covers the JSON signup's human check, the trial waiting for a confirmed email
and an install key, the service-wide daily AI token ceiling, and the portal
recipe upload charging its AI call to the member's ledger."""
import re

import httpx
import pytest

from app import email as mailer
from app import turnstile, usage
from app.config import settings
from app.database import SessionLocal
from app.deps import utc_now_iso
from app.models import Account, Entitlement, UsageLedger


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
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_from", "noreply@forager.test")
    sent: list = []
    monkeypatch.setattr(mailer, "_connect", lambda: _FakeSMTP(sent))
    return sent


def _turnstile(monkeypatch, success):
    monkeypatch.setattr(settings, "turnstile_site_key", "site")
    monkeypatch.setattr(settings, "turnstile_secret", "secret")
    monkeypatch.setattr(turnstile, "transport", httpx.MockTransport(
        lambda req: httpx.Response(200, json={"success": success})))


def _trial_rows(email):
    db = SessionLocal()
    try:
        acct = db.query(Account).filter_by(email=email).first()
        return db.query(Entitlement).filter_by(account_id=acct.id,
                                               source="trial").all()
    finally:
        db.close()


# --- The human check -----------------------------------------------------------

def test_json_signup_needs_the_challenge_when_turnstile_is_on(client, monkeypatch):
    _turnstile(monkeypatch, success=False)
    r = client.post("/v1/accounts/signup", json={
        "email": "bot@gmail.com", "password": "abcdefghijk1",
        "install_key": "k1"})
    assert r.status_code == 400
    db = SessionLocal()
    try:
        assert db.query(Account).filter_by(email="bot@gmail.com").first() is None
    finally:
        db.close()


def test_json_signup_passes_with_a_good_challenge(client, monkeypatch):
    _turnstile(monkeypatch, success=True)
    r = client.post("/v1/accounts/signup", json={
        "email": "human@gmail.com", "password": "abcdefghijk1",
        "install_key": "k2", "turnstile_token": "tok"})
    assert r.status_code == 200


def test_json_signup_without_turnstile_configured_still_works(client):
    r = client.post("/v1/accounts/signup", json={
        "email": "selfhost@example.com", "password": "abcdefghijk1",
        "install_key": "k3"})
    assert r.status_code == 200
    assert r.json()["trial_granted"] is True


# --- The trial waits for a key and a confirmed email -----------------------------

def test_signup_without_install_key_gets_no_trial(client):
    r = client.post("/v1/accounts/signup", json={
        "email": "web@example.com", "password": "abcdefghijk1"})
    assert r.status_code == 200
    assert r.json()["trial_granted"] is False
    assert r.json()["trial_message"] == usage.TRIAL_NEEDS_APP_MESSAGE
    assert _trial_rows("web@example.com") == []


def test_trial_starts_only_after_email_verification(client, email_on):
    r = client.post("/signup", data={
        "email": "new@example.com", "password": "abcdefghijk1",
        "confirm_password": "abcdefghijk1", "install_key": "install-new"},
        follow_redirects=False)
    assert r.status_code == 303
    assert _trial_rows("new@example.com") == []
    link = re.search(r"token=(\S+)", email_on[-1].get_content()).group(1)
    assert client.get(f"/verify?token={link}").status_code == 200
    rows = _trial_rows("new@example.com")
    assert len(rows) == 1 and rows[0].plan == "trial"
    # Following the link again does not mint a second trial.
    client.get(f"/verify?token={link}")
    assert len(_trial_rows("new@example.com")) == 1


def test_json_signup_defers_trial_until_verified(client, email_on):
    r = client.post("/v1/accounts/signup", json={
        "email": "app@example.com", "password": "abcdefghijk1",
        "install_key": "install-app"})
    assert r.status_code == 200
    assert r.json()["trial_granted"] is False
    assert r.json()["trial_message"] == usage.TRIAL_PENDING_MESSAGE
    assert _trial_rows("app@example.com") == []


# --- The daily ceiling -----------------------------------------------------------

def _spend_today(tokens):
    db = SessionLocal()
    try:
        acct = Account(email="whale@example.com", password_hash="x",
                       created_at=utc_now_iso())
        db.add(acct)
        db.commit()
        db.add(UsageLedger(account_id=acct.id, month_key=usage.month_key(),
                           tokens=tokens, kind="food", created_at=utc_now_iso()))
        db.commit()
    finally:
        db.close()


def test_daily_ceiling_refuses_ai_with_a_clear_503(client, instance_token,
                                                   monkeypatch, caplog):
    monkeypatch.setattr(settings, "daily_token_ceiling", 1000)
    _spend_today(1000)
    with caplog.at_level("ERROR"):
        r = client.post("/v1/ai/analyze", data={"kind": "enrich", "text": "x"},
                        headers={"Authorization": f"Bearer {instance_token}"})
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "daily_ceiling"
    assert any("ceiling" in rec.getMessage() for rec in caplog.records)


def test_daily_ceiling_below_the_limit_lets_calls_through(client, instance_token,
                                                          monkeypatch):
    monkeypatch.setattr(settings, "daily_token_ceiling", 10_000_000)
    _spend_today(10)
    r = client.post("/v1/ai/analyze", data={"kind": "enrich", "text": "x"},
                    headers={"Authorization": f"Bearer {instance_token}"})
    assert r.status_code == 200


def test_daily_ceiling_zero_is_off(monkeypatch):
    monkeypatch.setattr(settings, "daily_token_ceiling", 0)
    _spend_today(10**9)
    db = SessionLocal()
    try:
        assert usage.daily_ceiling_reached(db) is False
    finally:
        db.close()


# --- Portal recipe upload is metered ---------------------------------------------

def _portal_login(client):
    r = client.post("/login", data={"email": "dan@example.com",
                                    "password": "hunter2222"},
                    follow_redirects=False)
    client.cookies.set("forager_session", r.cookies.get("forager_session"))


def test_portal_recipe_upload_charges_the_ledger(client, instance_token):
    client.get("/v1/instance/me",
               headers={"Authorization": f"Bearer {instance_token}"})  # check in
    _portal_login(client)
    up = client.post("/recipes/upload", data={
        "method": "manual", "title": "T", "ingredients": "1 egg",
        "steps": "Cook", "attribution": "me"})
    assert up.status_code == 200, up.text[:300]
    db = SessionLocal()
    try:
        rows = db.query(UsageLedger).filter_by(kind="recipe_upload").all()
        assert len(rows) == 1 and rows[0].tokens > 0
    finally:
        db.close()


def test_portal_recipe_upload_respects_the_daily_ceiling(client, instance_token,
                                                         monkeypatch):
    client.get("/v1/instance/me",
               headers={"Authorization": f"Bearer {instance_token}"})
    _portal_login(client)
    monkeypatch.setattr(settings, "daily_token_ceiling", 5)
    _spend_today(5)
    up = client.post("/recipes/upload", data={
        "method": "manual", "title": "T", "ingredients": "1 egg",
        "steps": "Cook", "attribution": "me"})
    assert up.status_code == 503
    assert "has reached its limit for today" in up.text


# --- Google signup carries the install key -------------------------------------

def _google(monkeypatch, email):
    from app.routers import oauth_google
    monkeypatch.setattr(settings, "google_client_id", "cid")
    monkeypatch.setattr(settings, "google_client_secret", "cs")

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at"})
        return httpx.Response(200, json={"email": email, "email_verified": True})
    monkeypatch.setattr(oauth_google, "transport", httpx.MockTransport(handler))


def _google_signup(client, query):
    from urllib.parse import parse_qs, urlsplit
    start = client.get(f"/auth/google/start?intent=signup{query}",
                       follow_redirects=False)
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    return client.get(f"/auth/google/callback?code=c&state={state}",
                      follow_redirects=False)


def test_google_signup_with_install_key_gets_the_trial(client, monkeypatch):
    _google(monkeypatch, "g1@gmail.com")
    assert _google_signup(client, "&install_key=install-g1").status_code == 303
    assert len(_trial_rows("g1@gmail.com")) == 1


def test_google_signup_without_install_key_gets_no_trial(client, monkeypatch):
    _google(monkeypatch, "g2@gmail.com")
    assert _google_signup(client, "").status_code == 303
    assert _trial_rows("g2@gmail.com") == []


def test_signup_page_google_link_keeps_the_install_key(client, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "cid")
    monkeypatch.setattr(settings, "google_client_secret", "cs")
    page = client.get("/signup?install_key=abc123").text
    assert "/auth/google/start?intent=signup&install_key=abc123" in page
