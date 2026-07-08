"""Passkey (WebAuthn / FIDO2) sign-in for the Forager account.

The browser-and-authenticator half is stubbed: the two verify wrappers in
webauthn_service stand in for a real device signing a challenge, so no
authenticator is needed. The pure rules (rp id / origin derivation, the
signature-counter replay check) are tested directly, and the ceremonies are
driven end to end through the portal with a TestClient.
"""
import json
import types

from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect

from app import webauthn_service as wa
from app.config import settings
from app.database import SessionLocal, _alembic_config
from app.main import app
from app.models import Account, WebAuthnCredential
from app import models  # noqa: F401 - registers models on Base.metadata

PORTAL_SIGNUP = {"email": "dan@example.com", "password": "hunter2222",
                 "confirm_password": "hunter2222"}


# --- Pure helpers ---------------------------------------------------------

def test_rp_id_and_origin_from_forwarded_headers():
    # Behind Caddy: the forwarded host/scheme win, and the id drops the port.
    req = types.SimpleNamespace(
        headers={"x-forwarded-proto": "https",
                 "x-forwarded-host": "forager.pantryraider.app"},
        url=types.SimpleNamespace(scheme="http", netloc="internal:8000"))
    rp_id, origin = wa.rp_id_and_origin(req)
    assert rp_id == "forager.pantryraider.app"
    assert origin == "https://forager.pantryraider.app"


def test_rp_id_and_origin_localhost_dev():
    # Dev with no proxy: fall back to the Host header, keep the port in the
    # origin but never in the id.
    req = types.SimpleNamespace(
        headers={"host": "localhost:8000"},
        url=types.SimpleNamespace(scheme="http", netloc="localhost:8000"))
    rp_id, origin = wa.rp_id_and_origin(req)
    assert rp_id == "localhost"
    assert origin == "http://localhost:8000"


def test_sign_count_replay_rule():
    assert wa.sign_count_ok(0, 0) is True     # counterless authenticator
    assert wa.sign_count_ok(5, 6) is True      # advanced: fine
    assert wa.sign_count_ok(5, 5) is False     # stuck: clone/replay
    assert wa.sign_count_ok(5, 4) is False     # went backwards: clone/replay


# --- Test doubles for the verify seam -------------------------------------

class _Reg:
    def __init__(self, cred_id=b"cred-abc", pub=b"pubkey-bytes", sign_count=0):
        self.credential_id = cred_id
        self.credential_public_key = pub
        self.sign_count = sign_count


class _Auth:
    def __init__(self, new_sign_count):
        self.new_sign_count = new_sign_count


def _signed_in_client():
    client = TestClient(app)
    resp = client.post("/signup", data=PORTAL_SIGNUP, follow_redirects=False)
    assert resp.status_code == 303
    return client


def _account_id(email="dan@example.com"):
    db = SessionLocal()
    try:
        return db.query(Account).filter_by(email=email).first().id
    finally:
        db.close()


def _insert_credential(account_id, credential_id, public_key="pubkey-bytes",
                       sign_count=0, transports="internal", nickname="Phone"):
    db = SessionLocal()
    try:
        db.add(WebAuthnCredential(
            account_id=account_id,
            credential_id=wa.bytes_to_base64url(credential_id.encode())
            if isinstance(credential_id, str) else credential_id,
            public_key=wa.bytes_to_base64url(public_key.encode()),
            sign_count=sign_count, transports=transports, nickname=nickname,
            created_at="2026-07-07T00:00:00+00:00"))
        db.commit()
    finally:
        db.close()


# --- Registration ceremony ------------------------------------------------

def test_register_begin_has_rp_id_and_excludes_existing(monkeypatch):
    client = _signed_in_client()
    acct = _account_id()
    existing_id = "already-here"
    _insert_credential(acct, existing_id)

    resp = client.post("/account/passkeys/register/begin")
    assert resp.status_code == 200
    options = json.loads(resp.json()["options"])
    # TestClient's host is "testserver", so that is the derived rp id.
    assert options["rp"]["id"] == "testserver"
    assert options["rp"]["name"] == "Forager"
    # The user handle is the stable per-account id.
    from webauthn.helpers import base64url_to_bytes
    assert base64url_to_bytes(options["user"]["id"]) == str(acct).encode()
    # The one already-registered credential is excluded so the device is not
    # enrolled twice.
    excluded = {c["id"] for c in options.get("excludeCredentials", [])}
    assert wa.bytes_to_base64url(existing_id.encode()) in excluded
    # A challenge cookie was stashed server-side.
    assert "forager_passkey_reg" in resp.cookies


def test_register_finish_stores_credential(monkeypatch):
    monkeypatch.setattr(wa, "verify_registration",
                        lambda **kw: _Reg(cred_id=b"new-cred", sign_count=1))
    client = _signed_in_client()
    client.post("/account/passkeys/register/begin")
    body = {"nickname": "My laptop",
            "credential": {"id": "x", "response": {"transports": ["internal"]}}}
    resp = client.post("/account/passkeys/register/finish", json=body)
    assert resp.status_code == 200 and resp.json()["ok"] is True

    db = SessionLocal()
    try:
        cred = db.query(WebAuthnCredential).filter_by(
            credential_id=wa.bytes_to_base64url(b"new-cred")).first()
        assert cred is not None
        assert cred.nickname == "My laptop"
        assert cred.transports == "internal"
        assert cred.sign_count == 1
        # Only the public key is stored, never a secret.
        assert cred.public_key == wa.bytes_to_base64url(b"pubkey-bytes")
    finally:
        db.close()
    # It shows up in the Security pane.
    assert "My laptop" in client.get("/account").text


def test_register_finish_rejects_bad_attestation(monkeypatch):
    def boom(**kw):
        raise ValueError("bad challenge or origin")
    monkeypatch.setattr(wa, "verify_registration", boom)
    client = _signed_in_client()
    client.post("/account/passkeys/register/begin")
    resp = client.post("/account/passkeys/register/finish",
                       json={"credential": {"id": "x", "response": {}}})
    assert resp.status_code == 400
    db = SessionLocal()
    try:
        assert db.query(WebAuthnCredential).count() == 0
    finally:
        db.close()


def test_register_finish_needs_a_stashed_challenge(monkeypatch):
    monkeypatch.setattr(wa, "verify_registration", lambda **kw: _Reg())
    client = _signed_in_client()
    # No begin call, so no challenge cookie: finish must refuse.
    resp = client.post("/account/passkeys/register/finish",
                       json={"credential": {"id": "x", "response": {}}})
    assert resp.status_code == 400


def test_register_requires_sign_in():
    client = TestClient(app)
    assert client.post("/account/passkeys/register/begin").status_code == 401


# --- Password and 2FA keep working after a passkey ------------------------

def test_password_login_still_works_with_a_passkey(monkeypatch):
    monkeypatch.setattr(wa, "verify_registration", lambda **kw: _Reg())
    client = _signed_in_client()
    client.post("/account/passkeys/register/begin")
    client.post("/account/passkeys/register/finish",
                json={"credential": {"id": "x", "response": {}}})
    # The password is untouched: a fresh browser still signs in with it.
    browser = TestClient(app)
    resp = browser.post("/login",
                        data={"email": "dan@example.com", "password": "hunter2222"},
                        follow_redirects=False)
    assert resp.headers["location"] == "/account"


# --- Authentication ceremony ----------------------------------------------

def test_auth_begin_lists_the_accounts_credentials():
    client = TestClient(app)
    _signed_in_client()  # creates the account so _account_id() resolves
    acct = _account_id()
    _insert_credential(acct, "login-cred")
    resp = client.post("/login/passkey/begin",
                       json={"email": "dan@example.com"})
    assert resp.status_code == 200
    options = json.loads(resp.json()["options"])
    assert options["rpId"] == "testserver"
    ids = {c["id"] for c in options.get("allowCredentials", [])}
    assert wa.bytes_to_base64url(b"login-cred") in ids
    assert "forager_passkey_auth" in resp.cookies


def test_auth_begin_unknown_email_is_usernameless():
    client = TestClient(app)
    resp = client.post("/login/passkey/begin",
                       json={"email": "nobody@example.com"})
    assert resp.status_code == 200
    options = json.loads(resp.json()["options"])
    # No account leaked: an empty allow list, the discoverable-passkey path.
    assert not options.get("allowCredentials")


def test_auth_finish_opens_a_session(monkeypatch):
    _signed_in_client()
    acct = _account_id()
    _insert_credential(acct, "login-cred", sign_count=1)
    monkeypatch.setattr(wa, "verify_authentication",
                        lambda **kw: _Auth(new_sign_count=2))

    browser = TestClient(app)
    browser.post("/login/passkey/begin", json={})
    cred_id = wa.bytes_to_base64url(b"login-cred")
    resp = browser.post("/login/passkey/finish",
                        json={"credential": {"id": cred_id, "rawId": cred_id,
                                             "type": "public-key",
                                             "response": {}}})
    assert resp.status_code == 200
    assert resp.json()["next"] == "/account"
    assert "forager_session" in resp.cookies
    # The session really works, and the counter advanced.
    assert "dan@example.com" in browser.get("/account").text
    db = SessionLocal()
    try:
        cred = db.query(WebAuthnCredential).filter_by(
            credential_id=cred_id).first()
        assert cred.sign_count == 2
        assert cred.last_used_at
    finally:
        db.close()


def test_auth_finish_rejects_wrong_challenge(monkeypatch):
    _signed_in_client()
    acct = _account_id()
    _insert_credential(acct, "login-cred")

    def boom(**kw):
        raise ValueError("challenge mismatch")
    monkeypatch.setattr(wa, "verify_authentication", boom)
    browser = TestClient(app)
    browser.post("/login/passkey/begin", json={})
    cred_id = wa.bytes_to_base64url(b"login-cred")
    resp = browser.post("/login/passkey/finish",
                        json={"credential": {"id": cred_id, "response": {}}})
    assert resp.status_code == 401
    assert "forager_session" not in resp.cookies


def test_auth_finish_rejects_sign_count_regression(monkeypatch):
    _signed_in_client()
    acct = _account_id()
    _insert_credential(acct, "login-cred", sign_count=5)
    # The authenticator reports a counter that did not advance: a clone.
    monkeypatch.setattr(wa, "verify_authentication",
                        lambda **kw: _Auth(new_sign_count=5))
    browser = TestClient(app)
    browser.post("/login/passkey/begin", json={})
    cred_id = wa.bytes_to_base64url(b"login-cred")
    resp = browser.post("/login/passkey/finish",
                        json={"credential": {"id": cred_id, "response": {}}})
    assert resp.status_code == 401
    assert "forager_session" not in resp.cookies


# --- Remove is scoped to the caller (IDOR-safe) ---------------------------

def test_remove_deletes_only_your_own_passkey():
    # Account A owns a passkey.
    a = _signed_in_client()
    a_id = _account_id("dan@example.com")
    _insert_credential(a_id, "a-cred", nickname="A key")
    db = SessionLocal()
    try:
        a_cred_pk = db.query(WebAuthnCredential).filter_by(
            account_id=a_id).first().id
    finally:
        db.close()

    # Account B signs in and tries to remove A's passkey by its id.
    b = TestClient(app)
    b.post("/signup", data={"email": "eve@example.com", "password": "hunter2222",
                            "confirm_password": "hunter2222"},
           follow_redirects=False)
    resp = b.post(f"/account/passkeys/{a_cred_pk}/remove",
                  follow_redirects=False)
    assert resp.status_code == 303
    # A's passkey is untouched.
    db = SessionLocal()
    try:
        assert db.get(WebAuthnCredential, a_cred_pk) is not None
    finally:
        db.close()

    # The real owner can remove it.
    a.post(f"/account/passkeys/{a_cred_pk}/remove", follow_redirects=False)
    db = SessionLocal()
    try:
        assert db.get(WebAuthnCredential, a_cred_pk) is None
    finally:
        db.close()


# --- Rate limiting --------------------------------------------------------

def test_begin_endpoints_are_rate_limited(monkeypatch):
    monkeypatch.setattr(settings, "passkey_rate_per_minute", 2)
    client = TestClient(app)
    ok1 = client.post("/login/passkey/begin", json={})
    ok2 = client.post("/login/passkey/begin", json={})
    blocked = client.post("/login/passkey/begin", json={})
    assert ok1.status_code == 200 and ok2.status_code == 200
    assert blocked.status_code == 429


# --- Migration applies -----------------------------------------------------

def test_migration_creates_passkey_tables(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pk.db'}")
    cfg = _alembic_config()
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    tables = set(inspect(engine).get_table_names())
    assert {"webauthn_credentials", "webauthn_challenges"} <= tables
