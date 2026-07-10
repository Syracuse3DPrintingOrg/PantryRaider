"""Rate-limit client identity and password policy (FoodAssistant-ovyu)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import ratelimit
from app.config import settings
from app.deps import client_ip
from app.security import (MIN_PASSWORD_LENGTH, email_is_disposable,
                          password_problem)


def _req(xff=None, peer="10.0.0.1"):
    headers = {"x-forwarded-for": xff} if xff is not None else {}
    return SimpleNamespace(headers=headers,
                           client=SimpleNamespace(host=peer) if peer else None)


def test_client_ip_uses_the_proxy_appended_entry():
    # Caddy appends the true client, so the rightmost entry is trustworthy;
    # a spoofed leftmost value must not be what we rate-limit on.
    assert client_ip(_req(xff="1.2.3.4")) == "1.2.3.4"
    assert client_ip(_req(xff="9.9.9.9, 1.2.3.4")) == "1.2.3.4"
    assert client_ip(_req(xff="spoofed, real-client")) == "real-client"


def test_client_ip_falls_back_to_peer_without_header():
    assert client_ip(_req(xff=None, peer="10.0.0.5")) == "10.0.0.5"
    assert client_ip(_req(xff="", peer="10.0.0.6")) == "10.0.0.6"
    assert client_ip(_req(xff=None, peer=None)) == "unknown"


def test_two_clients_get_independent_rate_windows():
    # The bug this guards: behind a proxy every request looked like one IP,
    # so one client's attempts throttled everyone. Distinct IPs must not.
    ratelimit.reset()
    assert ratelimit.allow("login:1.2.3.4", 1) is True
    assert ratelimit.allow("login:1.2.3.4", 1) is False   # same client capped
    assert ratelimit.allow("login:5.6.7.8", 1) is True    # different client free


def test_password_problem_rejects_short_common_and_email():
    assert password_problem("short1") is not None
    assert password_problem("x" * (MIN_PASSWORD_LENGTH - 1)) is not None
    assert password_problem("password123") is not None      # common
    assert password_problem("PassWord123") is not None      # common, case-insensitive
    assert password_problem("dan@example.com", "dan@example.com") is not None
    # A decent password passes.
    assert password_problem("k7-mango-lantern", "dan@example.com") is None


def test_signup_enforces_the_policy(client):
    weak = client.post("/v1/accounts/signup",
                       json={"email": "new@example.com", "password": "password123"})
    assert weak.status_code == 400
    short = client.post("/v1/accounts/signup",
                        json={"email": "new@example.com", "password": "short12"})
    assert short.status_code == 400
    ok = client.post("/v1/accounts/signup",
                     json={"email": "new@example.com", "password": "k7-mango-lantern"})
    assert ok.status_code == 200


# --- Disposable email blocklist -------------------------------------------

def test_email_is_disposable_matches_domain_case_insensitively():
    assert email_is_disposable("throwaway@mailinator.com") is True
    assert email_is_disposable("Burner@YOPMail.com") is True
    assert email_is_disposable("me@guerrillamail.org") is True
    assert email_is_disposable("real.person@gmail.com") is False
    assert email_is_disposable("no-at-sign") is False
    assert email_is_disposable("") is False


def test_json_signup_rejects_disposable_email(client):
    bad = client.post("/v1/accounts/signup",
                      json={"email": "temp@mailinator.com",
                            "password": "k7-mango-lantern"})
    assert bad.status_code == 400
    assert "non-temporary" in bad.json()["detail"]
    good = client.post("/v1/accounts/signup",
                       json={"email": "real@gmail.com",
                             "password": "k7-mango-lantern"})
    assert good.status_code == 200


def test_portal_signup_rejects_disposable_email(client):
    resp = client.post("/signup",
                       data={"email": "temp@guerrillamail.com",
                             "password": "k7-mango-lantern",
                             "confirm_password": "k7-mango-lantern"},
                       follow_redirects=False)
    assert resp.status_code == 400
    assert "non-temporary" in resp.text


# --- Honeypot -------------------------------------------------------------

def test_honeypot_blocks_portal_signup_without_leaking(client):
    resp = client.post("/signup",
                       data={"email": "bot@gmail.com",
                             "password": "k7-mango-lantern",
                             "confirm_password": "k7-mango-lantern",
                             "website": "http://spam.example"},
                       follow_redirects=False)
    # Not a 303 redirect: no account was created and no session issued.
    assert resp.status_code == 400
    assert "forager_session" not in resp.cookies
    # The generic message must not reveal why it was rejected. (The decoy
    # input is still in the re-rendered form; what must not leak is the
    # reason, so the error text stays a plain "something went wrong".)
    assert "Something went wrong" in resp.text
    for leak in ("honeypot", "trap"):
        assert leak not in resp.text.lower()
    # Signing in with those credentials fails: the account never existed.
    login = client.post("/login",
                        data={"email": "bot@gmail.com",
                              "password": "k7-mango-lantern"},
                        follow_redirects=False)
    assert login.status_code == 401


def test_honeypot_blocks_portal_login_without_leaking(client):
    client.post("/signup",
                data={"email": "dan@example.com", "password": "hunter2222",
                      "confirm_password": "hunter2222"},
                follow_redirects=False)
    client.cookies.clear()
    resp = client.post("/login",
                       data={"email": "dan@example.com", "password": "hunter2222",
                             "website": "x"},
                       follow_redirects=False)
    # Correct password, but the trap short-circuits before authentication.
    assert resp.status_code == 401
    assert "forager_session" not in resp.cookies
    # Same generic failure a wrong password shows; the reason does not leak.
    assert "did not match" in resp.text
    for leak in ("honeypot", "trap"):
        assert leak not in resp.text.lower()


# --- Per-account lockout --------------------------------------------------

def _account(client, email="lock@example.com", password="k7-mango-lantern"):
    from app.database import SessionLocal
    from app.models import Account
    assert client.post("/v1/accounts/signup",
                       json={"email": email, "password": password}).status_code == 200
    db = SessionLocal()
    try:
        return db.query(Account).filter_by(email=email).first().id
    finally:
        db.close()


def test_lockout_after_threshold_then_refuses_correct_password(client, monkeypatch):
    from app.routers.accounts import (ACCOUNT_LOCKED_MESSAGE, authenticate)
    from app.database import SessionLocal
    monkeypatch.setattr(settings, "account_lockout_threshold", 3)
    monkeypatch.setattr(settings, "account_lockout_minutes", 15)
    _account(client)
    db = SessionLocal()
    try:
        # Wrong password below the threshold: no lock yet.
        for _ in range(2):
            acct, locked = authenticate(db, "lock@example.com", "wrong-guess")
            assert acct is None and locked is None
        # The third wrong attempt trips the lock.
        acct, locked = authenticate(db, "lock@example.com", "wrong-guess")
        assert acct is None and locked is None
        # Now even the right password is refused while locked.
        acct, locked = authenticate(db, "lock@example.com", "k7-mango-lantern")
        assert acct is None
        assert locked == ACCOUNT_LOCKED_MESSAGE
    finally:
        db.close()


def test_lockout_unlocks_after_the_window(client, monkeypatch):
    from app.routers.accounts import authenticate
    from app.database import SessionLocal
    monkeypatch.setattr(settings, "account_lockout_threshold", 2)
    monkeypatch.setattr(settings, "account_lockout_minutes", 15)
    _account(client)
    db = SessionLocal()
    try:
        for _ in range(2):
            authenticate(db, "lock@example.com", "wrong-guess")
        # Locked right now.
        _, locked = authenticate(db, "lock@example.com", "k7-mango-lantern")
        assert locked is not None
        # Inject a time past the lockout window: the right password works and
        # the counter is cleared.
        later = (datetime.now(timezone.utc) + timedelta(minutes=20)
                 ).isoformat(timespec="seconds")
        acct, locked = authenticate(db, "lock@example.com", "k7-mango-lantern",
                                    now=later)
        assert acct is not None and locked is None
    finally:
        db.close()


def test_successful_login_resets_the_failure_counter(client, monkeypatch):
    from app.routers.accounts import authenticate
    from app.database import SessionLocal
    from app.models import Account
    monkeypatch.setattr(settings, "account_lockout_threshold", 3)
    _account(client)
    db = SessionLocal()
    try:
        authenticate(db, "lock@example.com", "wrong-guess")
        authenticate(db, "lock@example.com", "wrong-guess")
        # A correct login clears the run of failures.
        acct, locked = authenticate(db, "lock@example.com", "k7-mango-lantern")
        assert acct is not None and locked is None
        db.expire_all()
        assert db.query(Account).filter_by(email="lock@example.com").first().failed_logins == 0
        # So a fresh wrong attempt starts counting from zero, not from two.
        authenticate(db, "lock@example.com", "wrong-guess")
        db.expire_all()
        assert db.query(Account).filter_by(email="lock@example.com").first().failed_logins == 1
    finally:
        db.close()


def test_locked_account_login_endpoint_returns_429(client, monkeypatch):
    monkeypatch.setattr(settings, "account_lockout_threshold", 2)
    _account(client)
    for _ in range(2):
        client.post("/v1/accounts/login",
                    json={"email": "lock@example.com", "password": "wrong-guess"})
    # Right password, but the account is locked: refused with 429.
    resp = client.post("/v1/accounts/login",
                       json={"email": "lock@example.com",
                             "password": "k7-mango-lantern"})
    assert resp.status_code == 429
    assert "failed attempts" in resp.json()["detail"].lower()
