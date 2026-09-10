"""The Google device-unlock flow (flow=unlock + /v1/instance/verify-unlock).

A member whose Forager account was created with Google has no password, so
the app's login page cannot verify them through verify-login. flow=unlock
runs the same Google round-trip as flow=app but mints a purpose="unlock"
code that never creates an account, never mints an instance, and is only
answerable at verify-unlock, where the device's own bearer token pins which
account the code must match (FoodAssistant-cd34).
"""
from urllib.parse import parse_qs, urlsplit

import httpx

from app.config import settings
from app.database import SessionLocal
from app.models import Account, PairingCode
from app.routers import oauth_google
from app.security import totp_now

RETURN = "http://192.168.1.50:9284/ui/login"


def _google(monkeypatch, email="dan@example.com"):
    """Enable the feature and stand in for Google's endpoints, verifying the
    given email."""
    monkeypatch.setattr(settings, "google_client_id", "test-client-id")
    monkeypatch.setattr(settings, "google_client_secret", "test-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "google-at"})
        if request.url.host == "openidconnect.googleapis.com":
            return httpx.Response(200, json={"email": email,
                                             "email_verified": True})
        return httpx.Response(404)

    monkeypatch.setattr(oauth_google, "transport", httpx.MockTransport(handler))


def _start_and_callback(client, path):
    """Drive the browser round-trip: start, then come back with the code."""
    start = client.get(path, follow_redirects=False)
    assert start.status_code == 303
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    return client.get(f"/auth/google/callback?code=fake-code&state={state}",
                      follow_redirects=False)


def _unlock_code(client, ret=RETURN):
    """A minted unlock code, parsed off the redirect back to the device."""
    resp = _start_and_callback(
        client, f"/auth/google/start?flow=unlock&return_url={ret}")
    assert resp.status_code == 303
    target = urlsplit(resp.headers["location"])
    assert target.path == "/ui/login"
    return parse_qs(target.query)["code"][0]


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def _verify(client, instance_token, code):
    return client.post("/v1/instance/verify-unlock",
                       headers=_headers(instance_token), json={"code": code})


# --- the happy path ----------------------------------------------------------

def test_unlock_confirms_the_linked_account(client, instance_token, monkeypatch):
    _google(monkeypatch)  # dan@example.com, the instance_token's own account
    code = _unlock_code(client)
    r = _verify(client, instance_token, code)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "account_email": "dan@example.com"}

    # Single use: the code died on redemption.
    again = _verify(client, instance_token, code)
    assert again.status_code == 401
    assert again.json() == {"error": "invalid_code"}


def test_unlock_code_carries_its_purpose(client, instance_token, monkeypatch):
    _google(monkeypatch)
    _unlock_code(client)
    db = SessionLocal()
    try:
        # The instance_token fixture already spent one "link" code; the fresh
        # unlock mint is the unredeemed row.
        row = db.query(PairingCode).filter_by(redeemed=0).one()
        assert row.purpose == "unlock"
    finally:
        db.close()


# --- the acceptance check: the account must be the linked one -----------------

def test_unlock_refuses_a_mismatched_account(client, instance_token, monkeypatch):
    # A real Forager member (eve) completes Google on dan's device: refused
    # with a distinct answer, and the code burns on that first presentation.
    client.post("/v1/accounts/signup",
                json={"email": "eve@example.com", "password": "hunter3333"})
    _google(monkeypatch, email="eve@example.com")
    code = _unlock_code(client)
    r = _verify(client, instance_token, code)
    assert r.status_code == 401
    assert r.json() == {"error": "account_mismatch"}
    # Burned on the mismatch: it cannot be probed again anywhere.
    again = _verify(client, instance_token, code)
    assert again.json() == {"error": "invalid_code"}


def test_unlock_never_creates_an_account(client, monkeypatch):
    # A Google sign-in for an address no Forager account uses must not create
    # one (unlike flow=app onboarding): the browser is sent back to the
    # device's login page with a friendly flag and no code.
    _google(monkeypatch, email="stranger@example.com")
    resp = _start_and_callback(
        client, f"/auth/google/start?flow=unlock&return_url={RETURN}")
    assert resp.status_code == 303
    target = urlsplit(resp.headers["location"])
    assert target.path == "/ui/login"
    q = parse_qs(target.query)
    assert q.get("error") == ["no-account"]
    assert "code" not in q
    db = SessionLocal()
    try:
        assert db.query(Account).filter_by(
            email="stranger@example.com").first() is None
        assert db.query(PairingCode).count() == 0
    finally:
        db.close()


def test_unlock_ignores_a_smuggled_signup_intent(client, monkeypatch):
    # intent=signup makes the portal and app flows create accounts; a device
    # unlock must never create one no matter what rode along on the start URL.
    _google(monkeypatch, email="stranger@example.com")
    resp = _start_and_callback(
        client,
        f"/auth/google/start?flow=unlock&intent=signup&return_url={RETURN}")
    assert resp.status_code == 303
    assert parse_qs(urlsplit(resp.headers["location"]).query).get(
        "error") == ["no-account"]
    db = SessionLocal()
    try:
        assert db.query(Account).filter_by(
            email="stranger@example.com").first() is None
    finally:
        db.close()


def test_unlock_code_for_a_disabled_account_collapses_to_invalid(
        client, instance_token, monkeypatch):
    _google(monkeypatch)
    code = _unlock_code(client)
    db = SessionLocal()
    try:
        account = db.query(Account).filter_by(email="dan@example.com").first()
        account.disabled = 1
        db.commit()
    finally:
        db.close()
    r = _verify(client, instance_token, code)
    assert r.status_code == 401
    assert r.json() == {"error": "invalid_code"}


# --- purpose separation: unlock and link codes never swap ---------------------

def test_unlock_code_cannot_mint_an_instance(client, instance_token, monkeypatch):
    _google(monkeypatch)
    code = _unlock_code(client)
    r = client.post("/v1/pairing/redeem", json={"code": code, "name": "Evil Pi"})
    assert r.status_code == 400
    # Refused like an unknown code, and it was not burned by the wrong door:
    # the rightful device can still spend it.
    ok = _verify(client, instance_token, code)
    assert ok.status_code == 200


def test_link_code_cannot_unlock_a_device(client, session_token, instance_token):
    code = client.post("/v1/pairing/code",
                       headers=_headers(session_token)).json()["code"]
    r = _verify(client, instance_token, code)
    assert r.status_code == 401
    assert r.json() == {"error": "invalid_code"}
    # And it still works for what it was minted for.
    assert client.post("/v1/pairing/redeem",
                       json={"code": code, "name": "Pi"}).status_code == 200


def test_expired_unlock_code_is_refused(client, instance_token, monkeypatch):
    _google(monkeypatch)
    code = _unlock_code(client)
    db = SessionLocal()
    try:
        row = db.query(PairingCode).filter_by(redeemed=0).one()
        row.expires_at = "2000-01-01T00:00:00+00:00"
        db.commit()
    finally:
        db.close()
    r = _verify(client, instance_token, code)
    assert r.status_code == 401
    assert r.json() == {"error": "invalid_code"}


# --- the guards shared with flow=app ------------------------------------------

def test_unlock_start_rejects_unsafe_return_urls(client, monkeypatch):
    _google(monkeypatch)
    for bad in ("https://evil.example.com/steal", "javascript:alert(1)",
                "not-a-url", ""):
        r = client.get("/auth/google/start",
                       params={"flow": "unlock", "return_url": bad},
                       follow_redirects=False)
        assert r.status_code == 400, bad


def test_unlock_refuses_a_foreign_kitchen_subdomain(client, instance_token,
                                                    monkeypatch):
    # Same FoodAssistant-5g4l account binding as flow=app: a tunnel subdomain
    # return_url must belong to the signing account, so a lure pointing at an
    # attacker's kitchen never receives dan's unlock code.
    from app.models import Instance, TunnelPeer
    db = SessionLocal()
    try:
        attacker = Account(email="attacker@example.com", password_hash="",
                           auth_provider="password", email_verified=1,
                           created_at="2026-01-01T00:00:00+00:00")
        db.add(attacker)
        db.commit()
        inst = Instance(token_hash="hash-evilkitchen", account_id=attacker.id,
                        name="evilkitchen",
                        created_at="2026-01-01T00:00:00+00:00")
        db.add(inst)
        db.commit()
        db.add(TunnelPeer(instance_id=inst.id, account_id=attacker.id,
                          tunnel_ip="10.99.0.9", subdomain="evilkitchen",
                          created_at="2026-01-01T00:00:00+00:00"))
        db.commit()
    finally:
        db.close()
    _google(monkeypatch)  # dan signs in
    ret = "https://evilkitchen.forager.pantryraider.app/ui/login"
    resp = _start_and_callback(
        client, f"/auth/google/start?flow=unlock&return_url={ret}")
    assert resp.status_code == 400
    db = SessionLocal()
    try:
        assert db.query(PairingCode).filter_by(redeemed=0).count() == 0
    finally:
        db.close()


def test_unlock_honours_account_2fa(client, instance_token, monkeypatch):
    # Google proves the email, not the second factor: a 2FA account lands on
    # the same code challenge the portal uses, and only a correct code sends
    # the browser back with an unlock-purpose code.
    from app.security import generate_totp_secret
    secret = generate_totp_secret()
    db = SessionLocal()
    try:
        account = db.query(Account).filter_by(email="dan@example.com").first()
        account.totp_secret = secret
        account.totp_enabled = 1
        db.commit()
    finally:
        db.close()

    _google(monkeypatch)
    resp = _start_and_callback(
        client, f"/auth/google/start?flow=unlock&return_url={RETURN}")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login/2fa"
    assert "code=" not in resp.headers["location"]  # nothing leaked yet

    done = client.post("/login/2fa", data={"code": totp_now(secret)},
                       follow_redirects=False)
    assert done.status_code == 303
    target = urlsplit(done.headers["location"])
    assert target.path == "/ui/login"
    code = parse_qs(target.query)["code"][0]

    # The challenge kept the unlock purpose: the code unlocks and cannot link.
    assert client.post("/v1/pairing/redeem",
                       json={"code": code}).status_code == 400
    ok = _verify(client, instance_token, code)
    assert ok.status_code == 200
    assert ok.json()["account_email"] == "dan@example.com"


# --- the endpoint's own gates --------------------------------------------------

def test_verify_unlock_requires_an_instance_token(client):
    r = client.post("/v1/instance/verify-unlock",
                    headers=_headers("prc_not_a_real_token"),
                    json={"code": "ABCD2345"})
    assert r.status_code == 401


def test_verify_unlock_rate_limited_per_instance(client, instance_token,
                                                 monkeypatch):
    from app import ratelimit
    ratelimit.reset()
    monkeypatch.setattr("app.config.settings.login_rate_per_minute", 3)
    seen_429 = False
    for _ in range(6):
        r = _verify(client, instance_token, "WRONG234")
        if r.status_code == 429:
            seen_429 = True
    assert seen_429
