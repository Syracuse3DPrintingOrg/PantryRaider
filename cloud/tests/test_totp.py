"""Two-factor sign-in (TOTP): the pure helpers, the enrollment and challenge
flows through the portal, and the JSON login / provision contract.

The pure tests pin the RFC 6238 published vectors; the flow tests drive the
real routes with a TestClient, pulling the secret and recovery codes straight
out of the rendered pages the way a person would read them off the screen."""
import base64
import re

import httpx
import pytest

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.routers import oauth_google
from app.security import (generate_recovery_codes, generate_totp_secret,
                          normalize_recovery_code, otpauth_uri, totp_now,
                          totp_verify)

# The RFC 6238 test secret: the ASCII string "12345678901234567890" in base32.
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()

PORTAL_SIGNUP = {"email": "dan@example.com", "password": "hunter2222",
                 "confirm_password": "hunter2222"}


# --- Pure helpers ---------------------------------------------------------

def test_totp_matches_rfc6238_vectors():
    # SHA-1, 30-second step, truncated to the low 6 digits of the RFC's 8.
    assert totp_now(RFC_SECRET, for_time=59) == "287082"
    assert totp_now(RFC_SECRET, for_time=1111111109) == "081804"


def test_totp_verify_accepts_current_code():
    secret = generate_totp_secret()
    assert totp_verify(secret, totp_now(secret, for_time=10_000), now=10_000)


def test_totp_verify_allows_clock_skew_within_window():
    secret = generate_totp_secret()
    # A code from the previous and next 30-second step still passes at window=1.
    prev = totp_now(secret, for_time=10_000 - 30)
    nxt = totp_now(secret, for_time=10_000 + 30)
    assert totp_verify(secret, prev, now=10_000, window=1)
    assert totp_verify(secret, nxt, now=10_000, window=1)
    # Two steps away is outside the default window.
    far = totp_now(secret, for_time=10_000 - 60)
    assert not totp_verify(secret, far, now=10_000, window=1)


def test_totp_verify_rejects_wrong_and_malformed():
    secret = generate_totp_secret()
    current = totp_now(secret, for_time=10_000)
    # A different 6-digit value (the current one plus one, wrapped) fails.
    wrong = f"{(int(current) + 1) % 1_000_000:06d}"
    assert not totp_verify(secret, wrong, now=10_000)
    assert not totp_verify(secret, "", now=10_000)
    assert not totp_verify(secret, "abcdef", now=10_000)
    assert not totp_verify(secret, "12345", now=10_000)
    assert not totp_verify(secret, "1234567", now=10_000)


def test_otpauth_uri_shape():
    uri = otpauth_uri("ABC234", "dan@example.com")
    assert uri.startswith("otpauth://totp/Forager:dan%40example.com?")
    assert "secret=ABC234" in uri
    assert "issuer=Forager" in uri
    assert "period=30" in uri and "digits=6" in uri


def test_recovery_codes_are_readable_and_normalise():
    codes = generate_recovery_codes()
    assert len(codes) == 10
    for c in codes:
        assert re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", c)
        # No lookalike characters.
        assert "O" not in c and "0" not in c and "1" not in c and "I" not in c
    # Normalisation strips the dash and case so typing is forgiving.
    assert normalize_recovery_code("abcd-2345") == "ABCD2345"
    assert normalize_recovery_code(" ab cd 23 45 ") == "ABCD2345"


# --- Portal enrollment and challenge --------------------------------------

_SECRET_RE = re.compile(r'name="secret" value="([A-Z0-9]+)"')
_CODE_RE = re.compile(r"<code>([A-Z0-9]{4}-[A-Z0-9]{4})</code>")


def _portal_signup(client, data=PORTAL_SIGNUP):
    resp = client.post("/signup", data=data, follow_redirects=False)
    assert resp.status_code == 303


def _enroll(client):
    """Turn on 2FA for the signed-in client and return (secret, codes)."""
    page = client.get("/account/2fa/setup")
    assert page.status_code == 200
    secret = _SECRET_RE.search(page.text).group(1)
    resp = client.post("/account/2fa/enable",
                       data={"secret": secret, "code": totp_now(secret)})
    assert resp.status_code == 200
    codes = _CODE_RE.findall(resp.text)
    assert len(codes) == 10
    return secret, codes


def test_enable_requires_a_valid_code():
    client = TestClient(app)
    _portal_signup(client)
    page = client.get("/account/2fa/setup")
    secret = _SECRET_RE.search(page.text).group(1)
    # A wrong code does not turn 2FA on, and keeps the same pending secret.
    bad = client.post("/account/2fa/enable",
                      data={"secret": secret, "code": "000000"})
    assert bad.status_code == 400
    assert secret in bad.text
    assert "Two-factor authentication is on" not in client.get("/account").text
    # The correct code turns it on and reveals the recovery codes once.
    secret2, codes = _enroll(client)
    assert client.get("/account").text.count("Turn off two-factor") >= 1
    # The secret is never shown again on the account page.
    assert secret2 not in client.get("/account").text


def test_login_requires_the_second_factor():
    setup = TestClient(app)
    _portal_signup(setup)
    secret, _ = _enroll(setup)

    # A fresh browser: correct password lands on the code page, not a session.
    browser = TestClient(app)
    resp = browser.post("/login", data={"email": "dan@example.com",
                                        "password": "hunter2222"},
                        follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login/2fa"
    assert "forager_session" not in resp.cookies
    # Abandoning the challenge leaves the browser signed out.
    assert browser.get("/account", follow_redirects=False
                       ).headers["location"] == "/login"
    # A correct code completes the sign-in.
    done = browser.post("/login/2fa", data={"code": totp_now(secret)},
                        follow_redirects=False)
    assert done.status_code == 303
    assert done.headers["location"] == "/account"
    assert "dan@example.com" in browser.get("/account").text


def test_login_wrong_code_does_not_sign_in():
    setup = TestClient(app)
    _portal_signup(setup)
    _enroll(setup)

    browser = TestClient(app)
    browser.post("/login", data={"email": "dan@example.com",
                                 "password": "hunter2222"},
                 follow_redirects=False)
    bad = browser.post("/login/2fa", data={"code": "000000"},
                       follow_redirects=False)
    assert bad.status_code == 401
    assert browser.get("/account", follow_redirects=False
                       ).headers["location"] == "/login"


def test_recovery_code_completes_login_and_is_single_use():
    setup = TestClient(app)
    _portal_signup(setup)
    _secret, codes = _enroll(setup)
    code = codes[0]

    browser = TestClient(app)
    browser.post("/login", data={"email": "dan@example.com",
                                 "password": "hunter2222"},
                 follow_redirects=False)
    done = browser.post("/login/2fa", data={"code": code},
                        follow_redirects=False)
    assert done.headers["location"] == "/account"

    # The same recovery code is burned: it cannot be reused.
    again = TestClient(app)
    again.post("/login", data={"email": "dan@example.com",
                               "password": "hunter2222"},
               follow_redirects=False)
    reuse = again.post("/login/2fa", data={"code": code},
                       follow_redirects=False)
    assert reuse.status_code == 401


def test_disable_requires_proof_then_turns_off():
    client = TestClient(app)
    _portal_signup(client)
    secret, _ = _enroll(client)

    # A wrong credential leaves 2FA on.
    bad = client.post("/account/2fa/disable", data={"credential": "nope"},
                      follow_redirects=False)
    assert bad.headers["location"] == "/account?e=twofa-bad"
    assert "Turn off two-factor" in client.get("/account").text

    # The account password turns it off.
    ok = client.post("/account/2fa/disable",
                     data={"credential": "hunter2222"}, follow_redirects=False)
    assert ok.headers["location"] == "/account?m=twofa-disabled"
    assert "Turn on two-factor" in client.get("/account").text

    # And login no longer asks for a second factor.
    browser = TestClient(app)
    resp = browser.post("/login", data={"email": "dan@example.com",
                                        "password": "hunter2222"},
                        follow_redirects=False)
    assert resp.headers["location"] == "/account"


def test_disable_accepts_a_totp_code():
    client = TestClient(app)
    _portal_signup(client)
    secret, _ = _enroll(client)
    ok = client.post("/account/2fa/disable",
                     data={"credential": totp_now(secret)},
                     follow_redirects=False)
    assert ok.headers["location"] == "/account?m=twofa-disabled"


def test_regenerate_recovery_codes_invalidates_the_old_set():
    client = TestClient(app)
    _portal_signup(client)
    _secret, old = _enroll(client)
    resp = client.post("/account/2fa/recovery/regenerate")
    assert resp.status_code == 200
    new = _CODE_RE.findall(resp.text)
    assert len(new) == 10 and set(new).isdisjoint(old)

    # An old code no longer completes a login.
    browser = TestClient(app)
    browser.post("/login", data={"email": "dan@example.com",
                                 "password": "hunter2222"},
                 follow_redirects=False)
    assert browser.post("/login/2fa", data={"code": old[0]},
                        follow_redirects=False).status_code == 401


# --- Google (password-less) accounts ---------------------------------------

@pytest.fixture
def google(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "test-client-id")
    monkeypatch.setattr(settings, "google_client_secret", "test-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "google-at"})
        return httpx.Response(200, json={"email": "gina@example.com",
                                         "email_verified": True})

    monkeypatch.setattr(oauth_google, "transport", httpx.MockTransport(handler))


def _google_login(client):
    from urllib.parse import parse_qs, urlsplit
    start = client.get("/auth/google/start?intent=signup", follow_redirects=False)
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    resp = client.get(f"/auth/google/callback?code=x&state={state}",
                      follow_redirects=False)
    return resp


def test_google_account_enables_and_disables_with_a_code(client, google):
    # A Google account has a session but no password.
    assert _google_login(client).headers["location"] == "/account"
    secret, _ = _enroll(client)

    # With no password, disabling needs a code, not a password (there is none).
    secret_now = totp_now(secret)
    ok = client.post("/account/2fa/disable", data={"credential": secret_now},
                     follow_redirects=False)
    assert ok.headers["location"] == "/account?m=twofa-disabled"


def test_google_account_login_is_gated_by_2fa(client, google):
    _google_login(client)
    secret, _ = _enroll(client)

    # A fresh Google sign-in for the same account must clear the second factor.
    # The google fixture patched settings and transport process-wide, so this
    # new client sees the same stubbed Google.
    browser = TestClient(app)
    resp = _google_login(browser)
    assert resp.headers["location"] == "/login/2fa"
    assert "forager_session" not in resp.cookies
    done = browser.post("/login/2fa", data={"code": totp_now(secret)},
                        follow_redirects=False)
    assert done.headers["location"] == "/account"


# --- JSON login and provisioning contract ---------------------------------

def _json_enroll():
    """Sign up and enable 2FA over the portal, returning the TOTP secret."""
    client = TestClient(app)
    _portal_signup(client)
    secret, codes = _enroll(client)
    return secret, codes


def test_json_login_requires_totp_when_enabled():
    secret, codes = _json_enroll()
    api = TestClient(app)
    creds = {"email": "dan@example.com", "password": "hunter2222"}

    # Missing code: a machine-readable prompt to collect one.
    missing = api.post("/v1/accounts/login", json=creds)
    assert missing.status_code == 401
    assert missing.json() == {"error": "totp_required"}

    # Wrong code: distinct from missing, so the app can say it was wrong.
    wrong = api.post("/v1/accounts/login", json={**creds, "totp": "000000"})
    assert wrong.status_code == 401
    assert wrong.json() == {"error": "totp_invalid"}

    # Correct code: a real session token.
    good = api.post("/v1/accounts/login", json={**creds, "totp": totp_now(secret)})
    assert good.status_code == 200
    assert good.json()["session_token"].startswith("prs_")

    # A recovery code works too, and burns.
    rec = api.post("/v1/accounts/login", json={**creds, "totp": codes[0]})
    assert rec.status_code == 200
    reuse = api.post("/v1/accounts/login", json={**creds, "totp": codes[0]})
    assert reuse.json() == {"error": "totp_invalid"}


def test_json_login_ignores_totp_when_off():
    client = TestClient(app)
    _portal_signup(client)
    # 2FA never enabled: a stray totp field is ignored, login succeeds.
    resp = client.post("/v1/accounts/login",
                       json={"email": "dan@example.com", "password": "hunter2222",
                             "totp": "whatever"})
    assert resp.status_code == 200
    assert resp.json()["session_token"].startswith("prs_")


def test_provision_requires_totp_when_enabled():
    secret, _codes = _json_enroll()
    api = TestClient(app)
    base = {"email": "dan@example.com", "password": "hunter2222",
            "device_name": "Kitchen Pi"}

    missing = api.post("/v1/instances/provision", json=base)
    assert missing.status_code == 401
    assert missing.json() == {"error": "totp_required"}

    wrong = api.post("/v1/instances/provision", json={**base, "totp": "000000"})
    assert wrong.json() == {"error": "totp_invalid"}

    good = api.post("/v1/instances/provision",
                    json={**base, "totp": totp_now(secret)})
    assert good.status_code == 200
    assert good.json()["instance_token"].startswith("prc_")


def test_provision_ignores_totp_when_off():
    client = TestClient(app)
    _portal_signup(client)
    resp = client.post("/v1/instances/provision",
                       json={"email": "dan@example.com", "password": "hunter2222",
                             "device_name": "Pi", "totp": "irrelevant"})
    assert resp.status_code == 200
    assert resp.json()["instance_token"].startswith("prc_")


# --- Brute-force and replay hardening (FoodAssistant-c2om) -----------------

def test_totp_code_cannot_be_replayed():
    # totp_verify accepts a +/- one-step window, so a captured code is live for
    # ~90 seconds. It must still be single-use: once a code signs in, the very
    # same code fails on a second login even before its window rolls over.
    secret, _ = _json_enroll()
    api = TestClient(app)
    creds = {"email": "dan@example.com", "password": "hunter2222"}
    code = totp_now(secret)
    first = api.post("/v1/accounts/login", json={**creds, "totp": code})
    assert first.status_code == 200
    replay = api.post("/v1/accounts/login", json={**creds, "totp": code})
    assert replay.status_code == 401
    assert replay.json() == {"error": "totp_invalid"}


def test_wrong_totp_counts_toward_a_per_account_lockout(monkeypatch):
    # A run of wrong codes is capped per account, not just per IP, so an
    # attacker who already has the password cannot grind the six-digit code by
    # rotating IPs. After the threshold the code prompt is refused outright,
    # even for the correct code, until the window passes.
    monkeypatch.setattr(settings, "account_lockout_threshold", 3)
    monkeypatch.setattr(settings, "account_lockout_minutes", 15)
    secret, _ = _json_enroll()
    api = TestClient(app)
    creds = {"email": "dan@example.com", "password": "hunter2222"}
    for _ in range(3):
        bad = api.post("/v1/accounts/login", json={**creds, "totp": "000000"})
        assert bad.status_code == 401
        assert bad.json() == {"error": "totp_invalid"}
    # The second factor is locked now: even a correct code is refused.
    good = api.post("/v1/accounts/login",
                    json={**creds, "totp": totp_now(secret)})
    assert good.status_code == 401
    assert good.json() == {"error": "totp_invalid"}


def test_totp_lockout_does_not_block_a_pre_auth_attacker(monkeypatch):
    # The second-factor lock is a SEPARATE counter from the password lock, so a
    # wrong-password flood (which never reaches the code prompt) cannot lock a
    # member out of their own 2FA (coordination with FoodAssistant-gszf).
    monkeypatch.setattr(settings, "account_lockout_threshold", 3)
    secret, _ = _json_enroll()
    api = TestClient(app)
    creds = {"email": "dan@example.com", "password": "hunter2222"}
    for _ in range(5):
        api.post("/v1/accounts/login",
                 json={"email": "dan@example.com", "password": "wrong", "totp": ""})
    # The owner, with the right password and a fresh code, still signs in.
    good = api.post("/v1/accounts/login",
                    json={**creds, "totp": totp_now(secret)})
    assert good.status_code == 200
    assert good.json()["session_token"].startswith("prs_")
