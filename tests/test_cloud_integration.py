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


def test_unlink_clears_token_locally(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_old")
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
    assert "pair again" in d["error"].lower()


# --- rendering ----------------------------------------------------------------

def test_ai_pane_shows_pairing_when_unlinked(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "")
    monkeypatch.setattr(settings, "deployment_mode", "server")
    with patch.object(type(settings), "is_configured", lambda self: True):
        html = client.get("/setup").text
    assert 'id="cloud_pairing_code"' in html
    assert "Forager" in html
    assert 'id="cloud-status"' not in html


def test_ai_pane_shows_linked_state(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_tok")
    monkeypatch.setattr(settings, "deployment_mode", "server")
    with patch.object(type(settings), "is_configured", lambda self: True):
        html = client.get("/setup").text
    assert 'id="cloud-status"' in html
    assert 'id="cloud-usage-display"' in html
    assert 'id="cloud_pairing_code"' not in html
    # The stored token itself never renders into the page.
    assert "prc_tok" not in html
