"""No async route runs a query or a file write on the event loop.

Forager runs one event loop. A sync SQLAlchemy call made directly inside an
async route blocks it, and under load a query waiting for a pooled connection
froze the whole service (the AI proxy had this first; see
test_ai_proxy_off_loop.py). The passkey ceremonies, the Stripe webhook and the
upload routes are async only to read the request body; every query, commit,
image re-encode and file write after that goes to the threadpool.

The statement listener records whether each SQL statement ran on the loop, and
the file-write seams are wrapped to record the same, so a handler that slips
back to blocking the loop fails here.
"""
import asyncio
import hashlib
import hmac
import inspect
import io
import json
import time

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import event

from app import recipe_images
from app import webauthn_service as wa
from app.config import settings
from app.database import SessionLocal, get_engine
from app.forwarder import ForwardResult
from app.main import app
from app.models import Account, WebAuthnCredential
from app.routers import backup as backup_router
from app.routers import recipe_upload
from tests.conftest import activate_entitlement


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.fixture
def seen():
    """Every SQL statement and watched file write, tagged with whether it ran
    on the event loop."""
    log = []

    def listener(conn, cursor, statement, params, context, executemany):
        log.append((_on_loop(), " ".join(statement.split())[:60]))

    event.listen(get_engine(), "before_cursor_execute", listener)
    yield log
    event.remove(get_engine(), "before_cursor_execute", listener)


@pytest.fixture(autouse=True)
def watch_file_writes(seen, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "recipe_image_dir", str(tmp_path / "img"))
    monkeypatch.setattr(settings, "backup_storage_dir", str(tmp_path / "bk"))

    real_store = recipe_images.store_image

    def store(*a, **kw):
        seen.append((_on_loop(), "store_image"))
        return real_store(*a, **kw)

    monkeypatch.setattr(recipe_images, "store_image", store)

    real_dir = backup_router._account_dir

    def account_dir(*a, **kw):
        seen.append((_on_loop(), "backup write"))
        return real_dir(*a, **kw)

    monkeypatch.setattr(backup_router, "_account_dir", account_dir)


def _assert_off_loop(seen, *must_include):
    assert seen, "nothing was recorded at all"
    assert [what for loop, what in seen if loop] == []
    for prefix in must_include:
        assert any(what.startswith(prefix) for _l, what in seen), prefix


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (4, 3), (200, 40, 90)).save(buf, "JPEG")
    return buf.getvalue()


PORTAL_SIGNUP = {"email": "dan@example.com", "password": "hunter2222",
                 "confirm_password": "hunter2222"}


def _portal_client():
    c = TestClient(app)
    assert c.post("/signup", data=PORTAL_SIGNUP,
                  follow_redirects=False).status_code == 303
    return c


def _account_id():
    db = SessionLocal()
    try:
        return db.query(Account).filter_by(email="dan@example.com").first().id
    finally:
        db.close()


# --- Passkeys ----------------------------------------------------------------

class _Reg:
    credential_id = b"new-cred"
    credential_public_key = b"pubkey-bytes"
    sign_count = 1


class _Auth:
    new_sign_count = 2


def test_passkey_register_finish_is_off_loop(seen, monkeypatch):
    monkeypatch.setattr(wa, "verify_registration", lambda **kw: _Reg())
    browser = _portal_client()
    browser.post("/account/passkeys/register/begin")
    seen.clear()
    resp = browser.post("/account/passkeys/register/finish",
                        json={"credential": {"id": "x", "response": {}}})
    assert resp.status_code == 200
    _assert_off_loop(seen, "INSERT INTO webauthn_credentials")


def test_passkey_sign_in_is_off_loop(seen, monkeypatch):
    _portal_client()
    cred_id = wa.bytes_to_base64url(b"login-cred")
    db = SessionLocal()
    try:
        db.add(WebAuthnCredential(
            account_id=_account_id(), credential_id=cred_id,
            public_key=wa.bytes_to_base64url(b"pubkey-bytes"), sign_count=1,
            transports="internal", nickname="Phone",
            created_at="2026-07-07T00:00:00+00:00"))
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(wa, "verify_authentication", lambda **kw: _Auth())

    browser = TestClient(app)
    seen.clear()
    assert browser.post("/login/passkey/begin",
                        json={"email": "dan@example.com"}).status_code == 200
    _assert_off_loop(seen, "INSERT INTO webauthn_challenges")
    seen.clear()
    resp = browser.post("/login/passkey/finish",
                        json={"credential": {"id": cred_id, "rawId": cred_id,
                                             "type": "public-key",
                                             "response": {}}})
    assert resp.status_code == 200
    _assert_off_loop(seen, "UPDATE webauthn_credentials")


# --- Stripe webhook ------------------------------------------------------------

def test_stripe_webhook_is_off_loop(seen, client, session_token):
    event_body = {
        "id": "evt_off_loop", "type": "checkout.session.completed",
        "data": {"object": {"client_reference_id": str(_account_id()),
                            "customer": "cus_1", "subscription": "sub_1"}},
    }
    payload = json.dumps(event_body).encode()
    ts = int(time.time())
    sig = hmac.new(settings.stripe_webhook_secret.encode(),
                   f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    seen.clear()
    resp = client.post("/v1/stripe/webhook", content=payload,
                       headers={"Stripe-Signature": f"t={ts},v1={sig}"})
    assert resp.status_code == 200
    _assert_off_loop(seen, "INSERT INTO stripe_events")


# --- Uploads -------------------------------------------------------------------

def test_backup_upload_is_off_loop(seen, client, instance_token):
    activate_entitlement(plan="premium")
    seen.clear()
    resp = client.post("/v1/backup/upload",
                       files={"file": ("b.zip", io.BytesIO(b"zipdata"),
                                       "application/zip")},
                       headers=_auth(instance_token))
    assert resp.status_code == 200
    _assert_off_loop(seen, "INSERT INTO cloud_backups", "backup write")


RECIPE = {"title": "Weeknight Chili", "description": "A quick pot of chili.",
          "ingredients": ["1 lb beans", "1 onion"],
          "steps": ["Chop the onion.", "Simmer for an hour."],
          "attribution": "From my recipe box."}


def test_community_recipe_image_upload_is_off_loop(seen, client, instance_token):
    rid = client.post("/v1/recipes", json=RECIPE,
                      headers=_auth(instance_token)).json()["id"]
    seen.clear()
    resp = client.post(f"/v1/recipes/{rid}/image",
                       files={"file": ("p.jpg", _jpeg(), "image/jpeg")},
                       headers=_auth(instance_token))
    assert resp.status_code == 200
    _assert_off_loop(seen, "UPDATE community_recipes", "store_image")


def test_share_image_upload_is_off_loop(seen, client, session_token):
    share = client.post("/v1/recipes/shares", json={
        "title": "Sam's Chili", "ingredients": ["1 lb beans"],
        "steps": ["Simmer."], "attribution": "Sam"},
        headers=_auth(session_token)).json()["token"]
    seen.clear()
    resp = client.post(f"/v1/recipes/shares/{share}/image",
                       files={"file": ("p.jpg", _jpeg(), "image/jpeg")},
                       headers=_auth(session_token))
    assert resp.status_code == 200
    _assert_off_loop(seen, "UPDATE shared_recipes", "store_image")


def test_portal_recipe_upload_is_off_loop(seen, monkeypatch):
    browser = _portal_client()
    db = SessionLocal()
    try:
        db.query(Account).filter_by(email="dan@example.com").first() \
            .recipe_upload_authorized = 1
        db.commit()
    finally:
        db.close()

    class Fake:
        async def forward(self, kind, image_data, mime_type, text):
            draft = {"title": "Chili", "ingredients": ["1 lb beans"],
                     "steps": ["Simmer."]}
            return ForwardResult(result={"text": json.dumps(draft)}, tokens=42)

    monkeypatch.setattr(recipe_upload, "get_forwarder", lambda: Fake())
    seen.clear()
    resp = browser.post("/recipes/upload", data={
        "method": "manual", "title": "Chili", "ingredients": "1 lb beans",
        "steps": "Simmer.", "attribution": "From my grandmother."})
    assert resp.status_code == 200
    # The tokens were charged to the ledger, off the loop like the rest.
    _assert_off_loop(seen, "INSERT INTO usage_ledger")


# --- Every async route is one of the above -------------------------------------

# Async routes that have been checked to keep their database and file work off
# the event loop. A new async route belongs here only once it has its own test
# above (or in test_ai_proxy_off_loop.py); otherwise make it a plain def route
# and FastAPI runs it in the threadpool.
CHECKED_ASYNC_ROUTES = {
    ("POST", "/account/passkeys/register/finish"),
    ("POST", "/login/passkey/begin"),
    ("POST", "/login/passkey/finish"),
    ("POST", "/v1/stripe/webhook"),
    ("POST", "/v1/backup/upload"),
    ("POST", "/v1/recipes/{recipe_id}/image"),
    ("POST", "/v1/recipes/shares/{token}/image"),
    ("POST", "/recipes/upload"),
    ("POST", "/v1/ai/analyze"),
}


def test_every_async_route_is_checked():
    found = set()
    for route in app.routes:
        if isinstance(route, APIRoute) and inspect.iscoroutinefunction(
                route.endpoint):
            for method in route.methods:
                found.add((method, route.path))
    assert found - CHECKED_ASYNC_ROUTES == set()
