"""Forager integration (FoodAssistant-2nd1).

Covers the CloudProvider (request shape, result parsing, the 402 quota
mapping, and the unreachable path), the pairing routes (link / unlink /
status with a mocked cloud), and the settings plumbing (secret-listed,
saveable, deliberately not satellite-synced). All HTTP is served by
httpx.MockTransport or monkeypatched clients: no network, matching the
scaffold contract in cloud/app/routers/ without importing cloud/.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi import HTTPException

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

from app.config import settings, _SAVEABLE, SECRET_SETTING_KEYS, SATELLITE_PULL_FIELDS  # noqa: E402
from app.providers.cloud import CloudProvider  # noqa: E402


# --- settings plumbing ------------------------------------------------------

def test_cloud_settings_are_saveable_and_secret():
    assert "cloud_base_url" in _SAVEABLE
    assert "cloud_instance_token" in _SAVEABLE
    assert "cloud_instance_token" in SECRET_SETTING_KEYS


def test_cloud_link_is_not_satellite_synced():
    # Each install pairs itself (its own instance on the account), so the
    # token and base URL must never be pulled from the main server.
    assert "cloud_instance_token" not in SATELLITE_PULL_FIELDS
    assert "cloud_base_url" not in SATELLITE_PULL_FIELDS


def test_ai_configured_counts_a_linked_cloud(monkeypatch):
    monkeypatch.setattr(settings, "vision_provider", "cloud")
    monkeypatch.setattr(settings, "cloud_instance_token", "")
    assert not settings.ai_configured()
    assert not settings.cloud_linked()
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_token")
    assert settings.ai_configured()
    assert settings.cloud_linked()


def test_build_provider_cloud(monkeypatch):
    from app.dependencies import _build_provider
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_token")
    monkeypatch.setattr(settings, "cloud_base_url", "https://cloud.example")
    p = _build_provider("cloud")
    assert isinstance(p, CloudProvider)
    assert p.base_url == "https://cloud.example"
    monkeypatch.setattr(settings, "cloud_instance_token", "")
    with pytest.raises(RuntimeError):
        _build_provider("cloud")


# --- provider ----------------------------------------------------------------

def _provider(handler) -> CloudProvider:
    return CloudProvider("https://cloud.test", "prc_secret",
                         transport=httpx.MockTransport(handler))


def test_analyze_food_success(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = request.read()
        return httpx.Response(200, json={
            "result": {"name": "Roma tomatoes", "quantity": 3, "unit": "pieces",
                       "storage_type": "room_temp", "category": "Produce",
                       "confidence": 0.9},
            "tokens": 1234,
            "quota": {"used": 1234, "quota": 2000000, "remaining": 1998766,
                      "month": "2026-07"},
        })

    result = asyncio.run(_provider(handler).analyze_food(b"jpegbytes", "image/jpeg"))
    assert seen["url"] == "https://cloud.test/v1/ai/analyze"
    assert seen["auth"] == "Bearer prc_secret"
    # Multipart form per the scaffold's ai router: kind field + image file.
    assert b'name="kind"' in seen["body"] and b"food" in seen["body"]
    assert b'name="image"' in seen["body"] and b"jpegbytes" in seen["body"]
    assert result.items[0].name == "Roma tomatoes"
    assert result.items[0].quantity == 3
    # The cloud's token charge is mirrored into the local usage history.
    usage = json.loads((tmp_path / "ai_usage.json").read_text())
    assert usage["by_provider"]["cloud"] == 1234


def test_analyze_receipt_parses_items():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "result": {"store": "Wegmans", "purchase_date": "2026-07-01",
                       "items": [{"name": "Milk", "quantity": 1, "unit": "item",
                                  "storage_type": "refrigerated",
                                  "category": "Dairy", "confidence": 0.8}]},
            "tokens": 0,
        })

    result = asyncio.run(_provider(handler).analyze_receipt(b"img", "image/png"))
    assert result.store == "Wegmans"
    assert str(result.purchased_on) == "2026-07-01"
    assert result.items[0].name == "Milk"


def test_enrich_product_round_trip():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read()
        return httpx.Response(200, json={
            "result": {"name": "Dr Pepper Zero Sugar", "category": "Beverages",
                       "storage_type": "room_temp", "shelf_life_days": 270,
                       "brand": "Dr Pepper"},
            "tokens": 50,
        })

    out = asyncio.run(_provider(handler).enrich_product({"product_name": "zero sugar"}))
    # Text-only task: no file part, so httpx sends a urlencoded form.
    assert b"kind=enrich" in seen["body"]
    assert b"zero+sugar" in seen["body"]
    assert out["name"] == "Dr Pepper Zero Sugar"


def test_quota_402_maps_to_local_budget_gate_shape():
    # The proxy's structured 402 must surface exactly like the local
    # token-budget gate in routers/analyze.py: HTTPException(429, <message>).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"detail": {
            "error": "quota_exceeded", "used": 2000000, "quota": 2000000,
            "month": "2026-07"}})

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_provider(handler).analyze_food(b"x", "image/jpeg"))
    assert exc.value.status_code == 429
    assert "quota" in exc.value.detail.lower()
    assert "2,000,000" in exc.value.detail
    assert "2026-07" in exc.value.detail


def test_no_subscription_402():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"detail": {"error": "no_subscription"}})

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_provider(handler).analyze_food(b"x", "image/jpeg"))
    assert exc.value.status_code == 429
    assert "subscription" in exc.value.detail.lower()


def test_unreachable_cloud_is_a_clean_502():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_provider(handler).analyze_food(b"x", "image/jpeg"))
    assert exc.value.status_code == 502
    assert "reached" in exc.value.detail


def test_health_check_true_on_200_false_on_401():
    ok = _provider(lambda r: httpx.Response(200, json={"instance_id": 1}))
    assert asyncio.run(ok.health_check()) is True
    bad = _provider(lambda r: httpx.Response(401, json={"detail": "Invalid"}))
    assert asyncio.run(bad.health_check()) is False


def test_unsupported_tasks_return_none():
    # The scaffold proxies food / receipt / enrich only; everything else is
    # honestly unsupported until the cloud grows those endpoints.
    p = CloudProvider("https://cloud.test", "prc_secret")
    assert asyncio.run(p.generate_recipe("soup")) is None
    assert asyncio.run(p.extract_recipe(page_text="x")) is None
    assert asyncio.run(p.estimate_nutrition("apple")) is None
    assert asyncio.run(p.suggest_from_inventory(["egg"])) is None


# --- pairing routes -----------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app

    cwd = os.getcwd()
    os.chdir(_SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(settings, "cloud_base_url", "https://cloud.test")
    try:
        # The cloud routes live in Settings, a post-wizard surface, so the
        # setup-redirect middleware must see a configured install.
        with patch.object(type(settings), "is_configured", lambda self: True):
            yield TestClient(app)
    finally:
        os.chdir(cwd)


class _FakeAsyncClient:
    """Stand-in for httpx.AsyncClient serving canned cloud replies."""

    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error

    def __call__(self, *a, **k):   # constructor stand-in
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kwargs):
        self.post_url, self.post_kwargs = url, kwargs
        if self._error:
            raise self._error
        return self._response

    async def get(self, url, **kwargs):
        self.get_url, self.get_kwargs = url, kwargs
        if self._error:
            raise self._error
        return self._response

    async def delete(self, url, **kwargs):
        self.delete_url, self.delete_kwargs = url, kwargs
        if self._error:
            raise self._error
        return self._response


def _resp(status, body):
    return httpx.Response(status, json=body,
                          request=httpx.Request("POST", "https://cloud.test"))


def test_link_redeems_and_stores_token(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "")
    fake = _FakeAsyncClient(_resp(200, {"instance_token": "prc_new", "instance_id": 7}))
    from app.routers import setup as setup_router
    with patch.object(setup_router.httpx, "AsyncClient", fake):
        r = client.post("/setup/cloud/link", json={"code": "ABCD-1234"})
    assert r.json() == {"ok": True}
    assert fake.post_url == "https://cloud.test/v1/pairing/redeem"
    assert fake.post_kwargs["json"]["code"] == "ABCD-1234"
    assert settings.cloud_instance_token == "prc_new"
    saved = json.loads((Path(settings.data_dir) / "settings.json").read_text())
    assert saved["cloud_instance_token"] == "prc_new"


def test_link_rejected_code_is_honest(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "")
    fake = _FakeAsyncClient(_resp(400, {"detail": "Invalid or expired pairing code"}))
    from app.routers import setup as setup_router
    with patch.object(setup_router.httpx, "AsyncClient", fake):
        r = client.post("/setup/cloud/link", json={"code": "NOPE"})
    d = r.json()
    assert d["ok"] is False
    assert "Invalid or expired" in d["error"]
    assert settings.cloud_instance_token == ""


def test_link_unreachable_cloud_is_honest(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "")
    fake = _FakeAsyncClient(error=httpx.ConnectError("refused"))
    from app.routers import setup as setup_router
    with patch.object(setup_router.httpx, "AsyncClient", fake):
        r = client.post("/setup/cloud/link", json={"code": "ABCD"})
    d = r.json()
    assert d["ok"] is False
    assert "could not be reached" in d["error"]


def test_unlink_clears_token_and_revokes_on_cloud(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_old")
    fake = _FakeAsyncClient(_resp(200, {"ok": True}))
    from app.routers import setup as setup_router
    with patch.object(setup_router.httpx, "AsyncClient", fake):
        r = client.post("/setup/cloud/unlink")
    assert r.json() == {"ok": True}
    assert settings.cloud_instance_token == ""
    # The cloud-side revoke was attempted with the old credential.
    assert fake.delete_url == "https://cloud.test/v1/instance"
    assert fake.delete_kwargs["headers"]["Authorization"] == "Bearer prc_old"


def test_unlink_survives_unreachable_cloud(client, monkeypatch):
    # The revoke is best effort: unreachable must never block local unlink.
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_old")
    fake = _FakeAsyncClient(error=httpx.ConnectError("down"))
    from app.routers import setup as setup_router
    with patch.object(setup_router.httpx, "AsyncClient", fake):
        r = client.post("/setup/cloud/unlink")
    assert r.json() == {"ok": True}
    assert settings.cloud_instance_token == ""


def test_status_unlinked(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "")
    r = client.get("/setup/cloud/status")
    assert r.json() == {"ok": True, "linked": False}


def test_status_linked_proxies_instance_me(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_tok")
    fake = _FakeAsyncClient(_resp(200, {
        "instance_id": 3, "name": "Kitchen Pi",
        "entitlement": {"active": True, "plan": "starter", "quota": 2000000,
                        "used": 5000, "remaining": 1995000,
                        "over_quota": False, "month": "2026-07"}}))
    from app.routers import setup as setup_router
    with patch.object(setup_router.httpx, "AsyncClient", fake):
        r = client.get("/setup/cloud/status")
    d = r.json()
    assert fake.get_url == "https://cloud.test/v1/instance/me"
    assert fake.get_kwargs["headers"]["Authorization"] == "Bearer prc_tok"
    assert d["linked"] and d["reachable"] and d["valid"]
    assert d["name"] == "Kitchen Pi"
    assert d["entitlement"]["quota"] == 2000000


def test_status_survives_unreachable_cloud(client, monkeypatch):
    # The settings page must render regardless: unreachable is data, not a 5xx.
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_tok")
    fake = _FakeAsyncClient(error=httpx.ConnectError("down"))
    from app.routers import setup as setup_router
    with patch.object(setup_router.httpx, "AsyncClient", fake):
        r = client.get("/setup/cloud/status")
    d = r.json()
    assert r.status_code == 200
    assert d["ok"] and d["linked"] and d["reachable"] is False
    assert "could not be reached" in d["error"]
    # The token never leaks into the error text.
    assert "prc_tok" not in json.dumps(d)


def test_status_revoked_token(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_tok")
    fake = _FakeAsyncClient(_resp(401, {"detail": "Invalid instance token"}))
    from app.routers import setup as setup_router
    with patch.object(setup_router.httpx, "AsyncClient", fake):
        r = client.get("/setup/cloud/status")
    d = r.json()
    assert d["linked"] and d["reachable"] and d["valid"] is False
    assert "sign in again" in d["error"].lower()


# --- rendering ----------------------------------------------------------------

def test_ai_pane_shows_signin_when_unlinked(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "")
    monkeypatch.setattr(settings, "deployment_mode", "server")
    with patch.object(type(settings), "is_configured", lambda self: True):
        html = client.get("/setup").text
    # Primary state: the account sign-in form.
    assert 'id="cloud_email"' in html
    assert 'id="cloud_password"' in html
    assert 'id="cloud_kitchen_name"' in html
    # The pairing-code path survives under the Advanced toggle.
    assert 'id="cloud_pairing_code"' in html
    assert 'id="cloud-advanced-collapse"' in html
    # The Google button renders hidden; setup/cloud/meta reveals it.
    assert 'id="cloud-google-btn"' in html
    assert "Forager" in html
    assert 'id="cloud-status"' not in html


def test_wizard_offers_forager_first(client, monkeypatch):
    # Unconfigured install renders the wizard; its AI step leads with the
    # Forager sign-in and keeps the manual key providers below.
    monkeypatch.setattr(settings, "cloud_instance_token", "")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "deployment_mode", "server")
    with patch.object(type(settings), "is_configured", lambda self: False):
        html = client.get("/setup").text
    assert 'id="wiz_cloud_email"' in html
    assert 'id="wiz_cloud_password"' in html
    assert 'id="wiz_cloud_kitchen_name"' in html
    assert 'id="wiz_cloud-google-btn"' in html
    # Forager is the first choice in the provider list, and with no AI
    # configured it is the default.
    forager = html.index('value="cloud"')
    assert forager < html.index('value="gemini"')
    assert 'value="cloud" selected' in html
    # The manual providers are still offered.
    assert 'id="gemini_api_key"' in html


# --- account sign-in (FoodAssistant-t6ab) --------------------------------------

def _fresh_ai(monkeypatch):
    """No usable AI provider configured, no Forager link, no public URL."""
    monkeypatch.setattr(settings, "cloud_instance_token", "")
    monkeypatch.setattr(settings, "vision_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "enrich_provider", "")
    monkeypatch.setattr(settings, "qr_public_url", "")


def _signin(client, fake, payload=None):
    from app.routers import setup as setup_router
    with patch.object(setup_router.httpx, "AsyncClient", fake):
        return client.post("/setup/cloud/signin", json=payload or {
            "email": "cook@example.com", "password": "hunter2",
            "device_name": "Kitchen Pi"})


def test_signin_provisions_and_autocompletes_a_fresh_install(client, monkeypatch):
    _fresh_ai(monkeypatch)
    fake = _FakeAsyncClient(_resp(200, {
        "instance_token": "prc_new", "account_email": "cook@example.com",
        "plan": "starter", "quota": 2000000, "month_used": 0,
        "suggested_public_url": None}))
    r = _signin(client, fake)
    d = r.json()
    assert d["ok"] is True
    assert d["account_email"] == "cook@example.com"
    assert d["plan"] == "starter"
    assert d["providers_set"] is True
    assert fake.post_url == "https://cloud.test/v1/instances/provision"
    assert fake.post_kwargs["json"] == {
        "email": "cook@example.com", "password": "hunter2",
        "device_name": "Kitchen Pi"}
    # Auto-complete: token stored, Forager becomes the provider end to end.
    assert settings.cloud_instance_token == "prc_new"
    assert settings.vision_provider == "cloud"
    assert settings.enrich_provider == "cloud"
    # suggested_public_url is null today, so the QR address is untouched.
    assert settings.qr_public_url == ""
    saved = json.loads((Path(settings.data_dir) / "settings.json").read_text())
    assert saved["cloud_instance_token"] == "prc_new"
    assert saved["vision_provider"] == "cloud"
    # The password never lands in the saved settings.
    assert "hunter2" not in json.dumps(saved)


def test_signin_preserves_a_working_provider(client, monkeypatch):
    _fresh_ai(monkeypatch)
    monkeypatch.setattr(settings, "gemini_api_key", "g-key")  # gemini works
    fake = _FakeAsyncClient(_resp(200, {
        "instance_token": "prc_new", "account_email": "cook@example.com",
        "plan": "starter", "suggested_public_url": None}))
    d = _signin(client, fake).json()
    assert d["ok"] is True and d["providers_set"] is False
    assert settings.cloud_instance_token == "prc_new"
    # The user's own provider stays in charge; the card offers the switch.
    assert settings.vision_provider == "gemini"
    assert settings.enrich_provider == ""


def test_signin_applies_the_platform_web_address(client, monkeypatch):
    # Address alignment: a non-null suggested_public_url becomes the QR /
    # outward-link address so local and outside addresses match the platform.
    _fresh_ai(monkeypatch)
    fake = _FakeAsyncClient(_resp(200, {
        "instance_token": "prc_new", "account_email": "cook@example.com",
        "plan": "starter",
        "suggested_public_url": "https://my-kitchen.forager.pantryraider.app/"}))
    d = _signin(client, fake).json()
    assert d["ok"] is True
    assert d["public_url"] == "https://my-kitchen.forager.pantryraider.app"
    assert settings.qr_public_url == "https://my-kitchen.forager.pantryraider.app"


def test_signin_leaves_an_existing_web_address_alone(client, monkeypatch):
    _fresh_ai(monkeypatch)
    monkeypatch.setattr(settings, "qr_public_url", "https://pantry.example.com")
    fake = _FakeAsyncClient(_resp(200, {
        "instance_token": "prc_new", "suggested_public_url": None}))
    assert _signin(client, fake).json()["ok"] is True
    assert settings.qr_public_url == "https://pantry.example.com"


def test_signin_wrong_password_is_friendly(client, monkeypatch):
    _fresh_ai(monkeypatch)
    fake = _FakeAsyncClient(_resp(401, {"detail": "invalid credentials"}))
    d = _signin(client, fake).json()
    assert d["ok"] is False
    assert "did not match" in d["error"]
    assert settings.cloud_instance_token == ""
    assert settings.vision_provider == "gemini"


def test_signin_rate_limited_is_honest(client, monkeypatch):
    _fresh_ai(monkeypatch)
    fake = _FakeAsyncClient(_resp(429, {"detail": "slow down"}))
    d = _signin(client, fake).json()
    assert d["ok"] is False
    assert "Too many sign-in attempts" in d["error"]


def test_signin_unreachable_cloud_is_honest(client, monkeypatch):
    _fresh_ai(monkeypatch)
    fake = _FakeAsyncClient(error=httpx.ConnectError("refused"))
    d = _signin(client, fake).json()
    assert d["ok"] is False
    assert "could not be reached" in d["error"]
    assert settings.cloud_instance_token == ""


def test_signin_requires_email_and_password(client, monkeypatch):
    _fresh_ai(monkeypatch)
    d = client.post("/setup/cloud/signin",
                    json={"email": "", "password": ""}).json()
    assert d["ok"] is False and "email and password" in d["error"]


def test_signin_error_never_echoes_the_password(client, monkeypatch):
    # A weird cloud error whose detail contains the password must come back
    # scrubbed (same guarantee _safe_error gives API keys elsewhere).
    _fresh_ai(monkeypatch)
    fake = _FakeAsyncClient(_resp(500, {"detail": "boom hunter2 boom"}))
    d = _signin(client, fake).json()
    assert d["ok"] is False
    assert "hunter2" not in json.dumps(d)


def test_signin_totp_required_prompts_for_a_code(client, monkeypatch):
    # A 2FA account: the password is right but the cloud asks for a code. The
    # app must flag totp_prompt so the page reveals the code field, not blame
    # the password, and must not link yet.
    _fresh_ai(monkeypatch)
    fake = _FakeAsyncClient(_resp(401, {"error": "totp_required"}))
    d = _signin(client, fake).json()
    assert d["ok"] is False
    assert d["totp_prompt"] is True
    assert "authenticator app" in d["error"]
    assert "did not match" not in d["error"]  # not a wrong-password message
    assert settings.cloud_instance_token == ""


def test_signin_totp_invalid_says_the_code_was_wrong(client, monkeypatch):
    _fresh_ai(monkeypatch)
    fake = _FakeAsyncClient(_resp(401, {"error": "totp_invalid"}))
    d = _signin(client, fake, payload={
        "email": "cook@example.com", "password": "hunter2",
        "device_name": "Kitchen Pi", "totp": "000000"}).json()
    assert d["ok"] is False
    assert d["totp_prompt"] is True
    assert "did not match" in d["error"]
    assert settings.cloud_instance_token == ""


def test_signin_forwards_the_code_and_links(client, monkeypatch):
    # The resubmit carries the code through to the provision call, and a good
    # code links exactly like a no-2FA sign-in.
    _fresh_ai(monkeypatch)
    fake = _FakeAsyncClient(_resp(200, {
        "instance_token": "prc_new", "account_email": "cook@example.com",
        "plan": "starter", "suggested_public_url": None}))
    d = _signin(client, fake, payload={
        "email": "cook@example.com", "password": "hunter2",
        "device_name": "Kitchen Pi", "totp": "123456"}).json()
    assert d["ok"] is True
    assert fake.post_kwargs["json"]["totp"] == "123456"
    assert settings.cloud_instance_token == "prc_new"


def test_signin_omits_totp_when_none_given(client, monkeypatch):
    # No code typed: the provision body carries no totp field at all, so the
    # first attempt looks exactly like a plain password sign-in.
    _fresh_ai(monkeypatch)
    fake = _FakeAsyncClient(_resp(200, {
        "instance_token": "prc_new", "suggested_public_url": None}))
    _signin(client, fake)
    assert "totp" not in fake.post_kwargs["json"]


# --- Google sign-in (meta gate + return leg) ------------------------------------

def test_cloud_meta_reports_google_when_cloud_offers_it(client, monkeypatch):
    fake = _FakeAsyncClient(_resp(200, {"oauth_google": True}))
    from app.routers import setup as setup_router
    with patch.object(setup_router.httpx, "AsyncClient", fake):
        d = client.get("/setup/cloud/meta").json()
    assert d["oauth_google"] is True
    assert d["google_start_url"] == "https://cloud.test/auth/google/start"


def test_cloud_meta_degrades_to_no_button(client, monkeypatch):
    # Unreachable cloud, or a cloud without Google sign-in: no button, no error.
    from app.routers import setup as setup_router
    fake = _FakeAsyncClient(error=httpx.ConnectError("down"))
    with patch.object(setup_router.httpx, "AsyncClient", fake):
        d = client.get("/setup/cloud/meta").json()
    assert d["ok"] is True and d["oauth_google"] is False
    fake = _FakeAsyncClient(_resp(200, {"oauth_google": False}))
    with patch.object(setup_router.httpx, "AsyncClient", fake):
        d = client.get("/setup/cloud/meta").json()
    assert d["oauth_google"] is False


def test_oauth_return_redeems_and_autocompletes(client, monkeypatch):
    _fresh_ai(monkeypatch)
    fake = _FakeAsyncClient(_resp(200, {
        "instance_token": "prc_oauth", "account_email": "cook@example.com",
        "suggested_public_url": None}))
    from app.routers import setup as setup_router
    with patch.object(setup_router.httpx, "AsyncClient", fake):
        r = client.get("/setup/cloud/oauth-return",
                       params={"code": "OTC-123", "flow": "settings"},
                       follow_redirects=False)
    assert r.status_code in (302, 303)
    assert r.headers["location"].endswith("/setup#pane-scanning")
    # The one-time code was redeemed against the existing pairing endpoint.
    assert fake.post_url == "https://cloud.test/v1/pairing/redeem"
    assert fake.post_kwargs["json"]["code"] == "OTC-123"
    # Same auto-complete path as the password sign-in.
    assert settings.cloud_instance_token == "prc_oauth"
    assert settings.vision_provider == "cloud"


def test_oauth_return_wizard_flow_returns_to_the_wizard(client, monkeypatch):
    _fresh_ai(monkeypatch)
    fake = _FakeAsyncClient(_resp(200, {"instance_token": "prc_oauth"}))
    from app.routers import setup as setup_router
    with patch.object(setup_router.httpx, "AsyncClient", fake):
        r = client.get("/setup/cloud/oauth-return",
                       params={"code": "OTC-123", "flow": "wizard"},
                       follow_redirects=False)
    assert r.status_code in (302, 303)
    assert r.headers["location"].endswith("/setup?cloud=done")


def test_oauth_return_bad_code_is_a_friendly_retry(client, monkeypatch):
    _fresh_ai(monkeypatch)
    fake = _FakeAsyncClient(_resp(400, {"detail": "Invalid or expired pairing code"}))
    from app.routers import setup as setup_router
    with patch.object(setup_router.httpx, "AsyncClient", fake):
        r = client.get("/setup/cloud/oauth-return",
                       params={"code": "NOPE", "flow": "settings"},
                       follow_redirects=False)
    assert r.status_code in (302, 303)
    loc = r.headers["location"]
    assert "cloud_error=" in loc and "expired" in loc
    assert settings.cloud_instance_token == ""


def test_ai_pane_shows_linked_state(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_tok")
    monkeypatch.setattr(settings, "deployment_mode", "server")
    monkeypatch.setattr(settings, "vision_provider", "gemini")
    monkeypatch.setattr(settings, "qr_public_url",
                        "https://my-kitchen.forager.pantryraider.app")
    with patch.object(type(settings), "is_configured", lambda self: True):
        html = client.get("/setup").text
    assert 'id="cloud-status"' in html
    assert 'id="cloud-usage-display"' in html
    assert 'id="cloud_pairing_code"' not in html
    # Signed in but scanning stays on the user's provider: offer the switch.
    assert "cloudUseForager" in html
    # The kitchen's web address shows on the connected card.
    assert "kitchen's web address" in html
    assert "https://my-kitchen.forager.pantryraider.app" in html
    # The stored token itself never renders into the page.
    assert "prc_tok" not in html
