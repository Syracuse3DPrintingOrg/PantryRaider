"""Recipe sharing beyond the community catalog (FoodAssistant-l697).

Covers the share-link payload builder, the fail-soft cloud share client
(create/inbox/revoke over httpx.MockTransport), and the router surface:
creating a share link (with friendly cloud-error mapping and the public-base
rule for the photo URL), the schema.org JSON-LD export (including the
round-trip back through parse_recipe_file), the shared-with-you inbox, and
importing a shared recipe. No network: cloud calls are mocked or the client
functions are monkeypatched.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

from app.config import settings  # noqa: E402
from app.services import recipe_store, recipes_forager  # noqa: E402
from app.services.recipes_import import parse_recipe_file  # noqa: E402


# --- fixtures ---------------------------------------------------------------

@pytest.fixture
def linked(monkeypatch):
    """A linked install with no public base configured."""
    monkeypatch.setattr(settings, "cloud_base_url", "https://forager.test")
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_secret")
    monkeypatch.setattr(settings, "tunnel_url", "")
    monkeypatch.setattr(settings, "qr_public_url", "")


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _detail(**overrides) -> dict:
    """A native-store detail dict (the Mealie-shaped read recipe_store serves)."""
    base = {
        "id": None, "native_id": 5, "slug": "garlic-bread", "name": "Garlic Bread",
        "description": "Crusty and quick", "recipeYield": "4 servings",
        "totalTime": "25 minutes", "prepTime": "10 minutes", "cookTime": "15 minutes",
        "orgURL": "https://example.com/garlic-bread", "origin": "manual",
        "tags": [], "categories": [], "image": "/recipes/images/5",
        "recipeIngredient": [{"note": "1 loaf bread"}, {"note": "3 cloves garlic"}],
        "recipeInstructions": [{"text": "Mix the butter and garlic."},
                               {"text": "Toast until golden."}],
    }
    base.update(overrides)
    return base


# --- build_share_payload (pure) ----------------------------------------------

def test_build_share_payload_requires_name_and_attribution():
    with pytest.raises(ValueError):
        recipes_forager.build_share_payload({"ingredients": ["x"]}, "Dan")
    with pytest.raises(ValueError) as e:
        recipes_forager.build_share_payload(
            {"name": "Bread", "ingredients": ["flour"]}, "")
    assert "credit" in str(e.value).lower()


def test_build_share_payload_core_shape_omits_blanks():
    recipe = {"name": "Bread", "description": "Simple loaf",
              "ingredients": ["flour"], "instructions": ["mix", "bake"]}
    payload = recipes_forager.build_share_payload(recipe, "Dan")
    assert payload == {"title": "Bread", "description": "Simple loaf",
                       "ingredients": ["flour"], "steps": ["mix", "bake"],
                       "attribution": "Dan"}
    # No empty image_url, recipient, email_to, or message keys ride along.
    assert "image_url" not in payload and "recipient" not in payload


def test_build_share_payload_carries_recipient_and_message():
    recipe = {"name": "Bread", "ingredients": ["flour"], "instructions": ["bake"],
              "image_url": "https://pub.example/recipes/images/5"}
    payload = recipes_forager.build_share_payload(
        recipe, "Dan", recipient="amy@example.com",
        email_to="amy@example.com", message="  Try this!  ")
    assert payload["recipient"] == "amy@example.com"
    assert payload["email_to"] == "amy@example.com"
    assert payload["message"] == "Try this!"
    assert payload["image_url"] == "https://pub.example/recipes/images/5"


# --- cloud client (mocked transport, fail-soft) --------------------------------

def test_create_share_posts_and_returns_url(linked):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.read())
        return httpx.Response(201, json={"ok": True, "token": "t1",
                                         "url": "https://forager.test/r/t1"})

    out = asyncio.run(recipes_forager.create_share(
        {"title": "Bread", "attribution": "Dan"}, transport=_transport(handler)))
    assert seen["path"] == "/v1/recipes/shares"
    assert seen["auth"] == "Bearer prc_secret"
    assert out["status"] == 201 and out["token"] == "t1"
    assert out["url"] == "https://forager.test/r/t1"


def test_create_share_unlinked_and_unreachable_are_soft(linked, monkeypatch):
    def handler(request):
        raise httpx.ConnectError("no route")

    out = asyncio.run(recipes_forager.create_share({}, transport=_transport(handler)))
    assert out["status"] == 0 and out["url"] is None

    monkeypatch.setattr(settings, "cloud_instance_token", "")
    out = asyncio.run(recipes_forager.create_share({}))
    assert out["status"] == 401


def test_list_share_inbox_accepts_wrapped_and_bare_lists(linked):
    share = {"token": "t1", "title": "Chili", "attribution": "Sam"}

    def wrapped(request):
        assert request.url.path == "/v1/recipes/shares/inbox"
        return httpx.Response(200, json={"shares": [share]})

    def bare(request):
        return httpx.Response(200, json=[share])

    assert asyncio.run(recipes_forager.list_share_inbox(
        transport=_transport(wrapped))) == [share]
    assert asyncio.run(recipes_forager.list_share_inbox(
        transport=_transport(bare))) == [share]


def test_list_share_inbox_empty_on_failure_or_unlinked(linked, monkeypatch):
    def handler(request):
        return httpx.Response(500, json={"detail": "boom"})

    assert asyncio.run(recipes_forager.list_share_inbox(
        transport=_transport(handler))) == []
    monkeypatch.setattr(settings, "cloud_instance_token", "")
    assert asyncio.run(recipes_forager.list_share_inbox()) == []


def test_revoke_share_true_only_on_confirm(linked):
    def ok(request):
        assert request.url.path == "/v1/recipes/shares/t1/revoke"
        return httpx.Response(200, json={"ok": True})

    def down(request):
        raise httpx.ConnectError("no route")

    assert asyncio.run(recipes_forager.revoke_share("t1", transport=_transport(ok))) is True
    assert asyncio.run(recipes_forager.revoke_share("t1", transport=_transport(down))) is False
    assert asyncio.run(recipes_forager.revoke_share("", transport=_transport(ok))) is False


# --- router ------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app
    cwd = os.getcwd()
    os.chdir(_SERVICE)  # templates load relative to the service dir
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True)
    monkeypatch.setattr(settings, "recipe_source", "off")
    # Native library (no Mealie configured), no public base unless a test sets one.
    monkeypatch.setattr(settings, "recipes_backend", "native")
    monkeypatch.setattr(settings, "tunnel_url", "")
    monkeypatch.setattr(settings, "qr_public_url", "")
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _stub_detail(monkeypatch, detail=None):
    monkeypatch.setattr(recipe_store, "detail",
                        lambda db, slug: detail if detail is not None else None)


def _stub_create_share(monkeypatch, result):
    seen = {}

    async def fake(payload):
        seen["payload"] = payload
        return result

    monkeypatch.setattr(recipes_forager, "create_share", fake)
    return seen


def test_share_link_happy_path(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_secret")
    _stub_detail(monkeypatch, _detail())
    seen = _stub_create_share(monkeypatch, {
        "status": 201, "url": "https://forager.test/r/t1", "token": "t1", "error": None})
    r = client.post("/mealie/recipes/share-link",
                    json={"slug": "garlic-bread", "attribution": "Dan"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["url"] == "https://forager.test/r/t1"
    assert d["token"] == "t1"
    payload = seen["payload"]
    assert payload["title"] == "Garlic Bread"
    assert payload["ingredients"] == ["1 loaf bread", "3 cloves garlic"]
    assert payload["steps"] == ["Mix the butter and garlic.", "Toast until golden."]
    # No public base configured: the LAN-only photo path must not be sent.
    assert "image_url" not in payload


def test_share_link_includes_image_only_with_public_base(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_secret")
    monkeypatch.setattr(settings, "tunnel_url", "https://mykitchen.forager.app/")
    _stub_detail(monkeypatch, _detail())
    seen = _stub_create_share(monkeypatch, {
        "status": 201, "url": "https://forager.test/r/t2", "token": "t2", "error": None})
    r = client.post("/mealie/recipes/share-link",
                    json={"slug": "garlic-bread", "attribution": "Dan"})
    assert r.status_code == 200
    assert seen["payload"]["image_url"] == \
        "https://mykitchen.forager.app/recipes/images/5"


def test_share_link_sends_recipient_fields(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_secret")
    _stub_detail(monkeypatch, _detail())
    seen = _stub_create_share(monkeypatch, {
        "status": 200, "url": "https://forager.test/r/t3", "token": "t3", "error": None})
    r = client.post("/mealie/recipes/share-link", json={
        "slug": "garlic-bread", "attribution": "Dan",
        "recipient": "amy@example.com", "email_to": "amy@example.com",
        "message": "Dinner idea"})
    assert r.status_code == 200
    assert "email" in r.json()["message"].lower()
    assert seen["payload"]["recipient"] == "amy@example.com"
    assert seen["payload"]["message"] == "Dinner idea"


def test_share_link_requires_linked_account(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "")
    r = client.post("/mealie/recipes/share-link",
                    json={"slug": "garlic-bread", "attribution": "Dan"})
    assert r.status_code == 400
    assert "forager" in r.json()["detail"].lower()


def test_share_link_requires_attribution_and_known_recipe(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_secret")
    _stub_detail(monkeypatch, _detail())
    r = client.post("/mealie/recipes/share-link",
                    json={"slug": "garlic-bread", "attribution": ""})
    assert r.status_code == 422
    assert "credit" in r.json()["detail"].lower()
    _stub_detail(monkeypatch, None)
    r = client.post("/mealie/recipes/share-link",
                    json={"slug": "nope", "attribution": "Dan"})
    assert r.status_code == 404


@pytest.mark.parametrize("cloud_status,expected", [
    (429, 429),   # rate limited -> friendly slow-down
    (401, 400),   # stale token -> reconnect prompt
    (0, 502),     # unreachable -> soft failure
])
def test_share_link_maps_cloud_errors(client, monkeypatch, cloud_status, expected):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_secret")
    _stub_detail(monkeypatch, _detail())
    _stub_create_share(monkeypatch, {
        "status": cloud_status, "url": None, "token": None, "error": None})
    r = client.post("/mealie/recipes/share-link",
                    json={"slug": "garlic-bread", "attribution": "Dan"})
    assert r.status_code == expected


# --- export ------------------------------------------------------------------

def test_export_emits_schema_org_jsonld(client, monkeypatch):
    _stub_detail(monkeypatch, _detail())
    r = client.get("/mealie/recipes/export", params={"slug": "garlic-bread"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/ld+json")
    assert 'filename="garlic-bread.json"' in r.headers["content-disposition"]
    ld = json.loads(r.content)
    assert ld["@context"] == "https://schema.org" and ld["@type"] == "Recipe"
    assert ld["name"] == "Garlic Bread"
    assert ld["recipeIngredient"] == ["1 loaf bread", "3 cloves garlic"]
    assert ld["recipeInstructions"] == [
        {"@type": "HowToStep", "text": "Mix the butter and garlic."},
        {"@type": "HowToStep", "text": "Toast until golden."}]
    assert ld["recipeYield"] == "4 servings"
    assert ld["totalTime"] == "25 minutes"
    assert ld["url"] == "https://example.com/garlic-bread"


def test_export_round_trips_through_file_import(client, monkeypatch):
    _stub_detail(monkeypatch, _detail())
    r = client.get("/mealie/recipes/export", params={"slug": "garlic-bread"})
    assert r.status_code == 200
    parsed = parse_recipe_file("garlic-bread.json", r.content)
    assert parsed["name"] == "Garlic Bread"
    assert parsed["ingredients"] == ["1 loaf bread", "3 cloves garlic"]
    assert parsed["instructions"] == ["Mix the butter and garlic.",
                                      "Toast until golden."]
    assert parsed["description"] == "Crusty and quick"
    assert parsed["total_time"] == "25 minutes"


def test_export_unknown_recipe_404s(client, monkeypatch):
    _stub_detail(monkeypatch, None)
    r = client.get("/mealie/recipes/export", params={"slug": "nope"})
    assert r.status_code == 404
    r = client.get("/mealie/recipes/export")
    assert r.status_code == 400


# --- shared inbox + import -----------------------------------------------------

_SHARE = {"token": "t9", "title": "Sam's Chili", "description": "Hearty",
          "ingredients": ["1 lb beans"], "steps": ["Simmer for an hour."],
          "image_url": "", "attribution": "Sam", "created_at": "2026-07-11"}


def _stub_inbox(monkeypatch, shares):
    async def fake():
        return shares
    monkeypatch.setattr(recipes_forager, "list_share_inbox", fake)


def test_shared_inbox_lists_when_linked(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_secret")
    _stub_inbox(monkeypatch, [_SHARE])
    r = client.get("/mealie/recipes/shared-inbox")
    assert r.status_code == 200
    assert r.json()["shares"] == [_SHARE]


def test_shared_inbox_empty_when_unlinked(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "")

    async def must_not_run():
        raise AssertionError("cloud must not be hit when unlinked")

    monkeypatch.setattr(recipes_forager, "list_share_inbox", must_not_run)
    r = client.get("/mealie/recipes/shared-inbox")
    assert r.status_code == 200
    assert r.json()["shares"] == []


def test_import_shared_saves_native_with_credit(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_secret")
    _stub_inbox(monkeypatch, [_SHARE])
    saved = {}

    async def fake_native_save(db, parsed, *, source, source_url=None, image_url=None):
        saved["parsed"] = parsed
        saved["source"] = source
        return {"slug": "sams-chili", "name": parsed["name"]}

    from app.routers import mealie as mealie_router
    monkeypatch.setattr(mealie_router, "_native_save", fake_native_save)
    r = client.post("/mealie/recipes/import-shared", json={"token": "t9"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["slug"] == "sams-chili"
    assert saved["source"] == "forager"
    assert saved["parsed"]["name"] == "Sam's Chili"
    assert saved["parsed"]["ingredients"] == ["1 lb beans"]
    assert saved["parsed"]["instructions"] == ["Simmer for an hour."]
    # Whoever shared it stays credited on the saved copy.
    assert "Sam" in saved["parsed"]["description"]


def test_import_shared_unknown_token_404s(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_secret")
    _stub_inbox(monkeypatch, [_SHARE])
    r = client.post("/mealie/recipes/import-shared", json={"token": "gone"})
    assert r.status_code == 404


def test_import_shared_requires_linked_account(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "")
    r = client.post("/mealie/recipes/import-shared", json={"token": "t9"})
    assert r.status_code == 400


# --- SSRF hardening (project evaluation 2026-07-13) --------------------------

def test_import_url_refuses_loopback_targets(client):
    # No recipe lives on loopback; a crafted URL must not turn the import fetch
    # into a probe of same-host services (the host bridge on 127.0.0.1:9299).
    for url in ("http://127.0.0.1:9299/display/status",
                "http://localhost:9299/",
                "http://[::1]:8000/x",
                "http://169.254.1.1/recipe"):
        r = client.post("/mealie/recipes/import-url", json={"url": url})
        assert r.status_code == 400, url
        assert "points at this device" in r.json()["detail"]


def test_import_url_still_accepts_normal_hosts(client, monkeypatch):
    # A regular hostname passes the guard (the fetch itself is stubbed out).
    from app.services import recipe_scrape

    async def _boom(url):
        raise recipe_scrape.RecipeScrapeError("nope")
    monkeypatch.setattr(recipe_scrape, "scrape_url", _boom)
    # Fails later (no AI provider to extract with), but NOT with the guard's 400.
    r = client.post("/mealie/recipes/import-url",
                    json={"url": "http://example.com/recipe"})
    assert "points at this device" not in (r.json().get("detail") or "")


def test_public_image_url_drops_ip_literal_bases(monkeypatch):
    # The cloud refuses IP-literal image hosts, so an IP-literal public base
    # must yield no image rather than failing the whole share.
    from app.routers.mealie import _public_image_url
    from app.config import settings
    monkeypatch.setattr(settings, "tunnel_url", "", raising=False)
    monkeypatch.setattr(settings, "qr_public_url", "http://192.168.1.170:9284", raising=False)
    assert _public_image_url("/recipes/images/7") == ""
    monkeypatch.setattr(settings, "qr_public_url", "https://kitchen.example.com", raising=False)
    assert _public_image_url("/recipes/images/7") == "https://kitchen.example.com/recipes/images/7"
