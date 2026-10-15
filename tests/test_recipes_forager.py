"""Forager community recipes as a recipe source (FoodAssistant-l2hk, Stage 3a).

Covers the forager recipe client (normalizing a cloud card/detail into the
app's recipe shape with source="forager", and the submit payload builder), the
enable-setting gating (saveable, per-device, default-on when linked), and the
browse + share router flow with the cloud HTTP mocked. No network: every cloud
call is served by httpx.MockTransport.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx
import pytest

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

from app.config import settings, _SAVEABLE, SECRET_SETTING_KEYS, SATELLITE_PULL_FIELDS  # noqa: E402
from app.services import recipes_forager  # noqa: E402


# --- fixtures ---------------------------------------------------------------

@pytest.fixture
def linked(monkeypatch):
    """A linked, community-enabled install."""
    monkeypatch.setattr(settings, "cloud_base_url", "https://forager.test")
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_secret")
    monkeypatch.setattr(settings, "forager_recipes_enabled", True)


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# --- settings plumbing ------------------------------------------------------

def test_enable_setting_is_saveable_not_secret_not_synced():
    assert "forager_recipes_enabled" in _SAVEABLE
    # Not a credential.
    assert "forager_recipes_enabled" not in SECRET_SETTING_KEYS
    # Per-device like the pairing itself: never pulled from the main server.
    assert "forager_recipes_enabled" not in SATELLITE_PULL_FIELDS


def test_active_defaults_on_when_linked_and_gated_on_linkage(monkeypatch):
    # Default value is on.
    assert settings.forager_recipes_enabled is True
    # Linked + enabled -> community source is active.
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_secret")
    monkeypatch.setattr(settings, "forager_recipes_enabled", True)
    assert settings.forager_recipes_active() is True
    # Turned off -> inactive even while linked.
    monkeypatch.setattr(settings, "forager_recipes_enabled", False)
    assert settings.forager_recipes_active() is False
    # Not linked -> inactive regardless of the toggle (gated on linkage).
    monkeypatch.setattr(settings, "forager_recipes_enabled", True)
    monkeypatch.setattr(settings, "cloud_instance_token", "")
    assert settings.forager_recipes_active() is False


# --- normalization ----------------------------------------------------------

def test_normalize_card_to_app_shape():
    card = {"id": 42, "title": "Miso Soup", "description": "Warm and quick",
            "image_url": "https://img/x.jpg", "attribution": "Aki",
            "average_rating": 4.5, "rating_count": 12}
    out = recipes_forager._normalize_card(card)
    assert out["source"] == "forager"
    assert out["name"] == "Miso Soup"
    assert out["external_id"] == "42"
    assert out["image"] == "https://img/x.jpg"
    assert out["attribution"] == "Aki"
    assert out["average_rating"] == 4.5 and out["rating_count"] == 12


def test_normalize_detail_shape_and_attribution_credit():
    detail = {"id": 7, "title": "Chili", "description": "Hearty",
              "ingredients": ["1 lb beans", {"text": "2 onions"}],
              "steps": ["Chop", {"text": "Simmer"}],
              "attribution": "Sam", "image_url": "https://img/c.jpg"}
    out = recipes_forager._normalize_detail(detail)
    assert out["source"] == "forager"
    assert out["external_id"] == "7"
    assert out["ingredients"] == ["1 lb beans", "2 onions"]
    assert out["instructions"] == ["Chop", "Simmer"]
    # Tier classifier / Mealie save read recipeIngredient.
    assert out["recipeIngredient"] == [{"note": "1 lb beans"}, {"note": "2 onions"}]
    # Credit rides along in the description so it survives the Mealie save.
    assert "Sam" in out["description"]
    assert out["attribution"] == "Sam"


# --- submit payload builder -------------------------------------------------

def test_build_submit_payload_requires_attribution():
    recipe = {"name": "Bread", "ingredients": ["flour"], "instructions": ["bake"]}
    with pytest.raises(ValueError) as e:
        recipes_forager.build_submit_payload(recipe, "")
    assert "credit" in str(e.value).lower()


def test_build_submit_payload_requires_name():
    with pytest.raises(ValueError):
        recipes_forager.build_submit_payload({"ingredients": ["x"]}, "Dan")


def test_build_submit_payload_shape():
    recipe = {"name": "Bread", "description": "Simple loaf",
              "ingredients": ["flour", " "], "instructions": ["mix", "bake"],
              "image_url": "https://img/b.jpg"}
    payload = recipes_forager.build_submit_payload(recipe, "  Dan  ")
    assert payload == {
        "title": "Bread", "description": "Simple loaf",
        "ingredients": ["flour"], "steps": ["mix", "bake"],
        "attribution": "Dan", "image_url": "https://img/b.jpg",
    }


def test_build_submit_payload_reads_recipeingredient_fallback():
    # A Mealie recipe carries recipeIngredient, not a plain ingredients list.
    recipe = {"name": "Soup", "recipeIngredient": [{"note": "broth"}, {"note": ""}],
              "instructions": ["boil"]}
    payload = recipes_forager.build_submit_payload(recipe, "Ana")
    assert payload["ingredients"] == ["broth"]


# --- bundle helpers (pure) --------------------------------------------------

def test_partition_new_skips_present_and_dupes():
    cards = [
        {"name": "Alpha", "external_id": "1"},
        {"name": "beta", "external_id": "2"},
        {"name": "Beta", "external_id": "3"},   # in-batch repeat of "beta"
        {"name": "Gamma", "external_id": "4"},
        {"name": "", "external_id": "5"},        # no usable name
    ]
    # Existing library titles: case/whitespace-insensitive match.
    new, present = recipes_forager.partition_new(cards, ["  ALPHA "])
    assert [c["external_id"] for c in new] == ["2", "4"]   # beta, Gamma
    # Alpha (present), the Beta repeat, and the nameless card are all skipped.
    assert {c["external_id"] for c in present} == {"1", "3", "5"}


def test_partition_new_all_new_when_library_empty():
    cards = [{"name": "Alpha", "external_id": "1"}, {"name": "Beta", "external_id": "2"}]
    new, present = recipes_forager.partition_new(cards, [])
    assert len(new) == 2 and present == []


def test_format_bundle_summary_copy():
    assert recipes_forager.format_bundle_summary(1, 0, 0) == "Added 1 recipe."
    assert recipes_forager.format_bundle_summary(
        23, 4, 0) == "Added 23 recipes, skipped 4 already in your library."
    assert "could not be added" in recipes_forager.format_bundle_summary(2, 0, 1)
    assert recipes_forager.format_bundle_summary(0, 0, 0).startswith("No community recipes")


# --- client cloud calls (mocked, no network) --------------------------------

def test_search_recipes_normalizes_and_sends_token(linked):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"recipes": [
            {"id": 1, "title": "Miso Soup", "attribution": "Aki", "average_rating": 4.5},
            {"id": 2, "title": "Ramen", "attribution": "Ken"},
        ]})

    out = asyncio.run(recipes_forager.search_recipes("soup", transport=_transport(handler)))
    assert seen["auth"] == "Bearer prc_secret"
    assert "/v1/recipes" in seen["url"] and "query=soup" in seen["url"]
    assert [r["name"] for r in out] == ["Miso Soup", "Ramen"]
    assert all(r["source"] == "forager" for r in out)


def test_search_recipes_omitted_when_unlinked(monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "")
    monkeypatch.setattr(settings, "forager_recipes_enabled", True)

    def handler(request):  # must never be called
        raise AssertionError("cloud must not be hit when unlinked")

    out = asyncio.run(recipes_forager.search_recipes("x", transport=_transport(handler)))
    assert out == []


def test_search_recipes_fails_soft_on_cloud_error(linked):
    def handler(request):
        return httpx.Response(500, json={"detail": "boom"})

    out = asyncio.run(recipes_forager.search_recipes("x", transport=_transport(handler)))
    assert out == []


def test_get_recipe_normalizes_detail(linked):
    def handler(request):
        assert request.url.path == "/v1/recipes/7"
        return httpx.Response(200, json={
            "id": 7, "title": "Chili", "ingredients": ["beans"],
            "steps": ["simmer"], "attribution": "Sam"})

    out = asyncio.run(recipes_forager.get_recipe("7", transport=_transport(handler)))
    assert out["name"] == "Chili" and out["source"] == "forager"
    assert out["ingredients"] == ["beans"]


def test_submit_recipe_posts_payload_and_returns_id(linked):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["body"] = request.read()
        return httpx.Response(201, json={"id": "abc123"})

    payload = {"title": "Bread", "ingredients": ["flour"], "steps": ["bake"],
               "attribution": "Dan", "description": "", "image_url": ""}
    out = asyncio.run(recipes_forager.submit_recipe(payload, transport=_transport(handler)))
    assert seen["method"] == "POST"
    assert b"Dan" in seen["body"] and b"Bread" in seen["body"]
    assert out["status"] == 201 and out["id"] == "abc123"


def test_submit_recipe_unreachable_is_soft(linked):
    def handler(request):
        raise httpx.ConnectError("no route")

    out = asyncio.run(recipes_forager.submit_recipe(
        {"title": "x"}, transport=_transport(handler)))
    assert out["status"] == 0


# --- image upload (FIX A: Forager hosts its own copy of the photo) ----------

def test_upload_recipe_image_posts_bytes_and_returns_url(linked):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["method"] = request.method
        seen["auth"] = request.headers.get("Authorization")
        seen["ctype"] = request.headers.get("Content-Type", "")
        seen["body"] = request.read()
        return httpx.Response(200, json={"ok": True,
                                         "image_url": "https://forager.test/v1/recipes/7/image"})

    out = asyncio.run(recipes_forager.upload_recipe_image(
        "/v1/recipes/7/image", (b"\x89PNG\r\n\x1a\n bytes", "image/png"),
        transport=_transport(handler)))
    assert seen["method"] == "POST" and seen["path"] == "/v1/recipes/7/image"
    assert seen["auth"] == "Bearer prc_secret"
    assert seen["ctype"].startswith("multipart/form-data")  # a file upload
    assert b"\x89PNG" in seen["body"]
    assert out["status"] == 200
    assert out["image_url"] == "https://forager.test/v1/recipes/7/image"


def test_upload_recipe_image_degrades_when_no_image(linked):
    def handler(request):  # must never be called: nothing to upload
        raise AssertionError("no request when there is no image")

    out = asyncio.run(recipes_forager.upload_recipe_image(
        "/v1/recipes/7/image", None, transport=_transport(handler)))
    assert out["status"] == 0 and out["image_url"] is None


def test_upload_recipe_image_unlinked_and_unreachable_are_soft(linked, monkeypatch):
    def down(request):
        raise httpx.ConnectError("no route")

    out = asyncio.run(recipes_forager.upload_recipe_image(
        "/v1/recipes/7/image", (b"jpgbytes...", "image/jpeg"),
        transport=_transport(down)))
    assert out["status"] == 0  # unreachable never raises; the share still went

    monkeypatch.setattr(settings, "cloud_instance_token", "")
    out = asyncio.run(recipes_forager.upload_recipe_image(
        "/v1/recipes/7/image", (b"jpgbytes...", "image/jpeg")))
    assert out["status"] == 401


# --- router: browse + share -------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app
    cwd = os.getcwd()
    os.chdir(_SERVICE)  # templates load relative to the service dir
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_password", "")
    # Past the first-run setup redirect so requests reach the router.
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True)
    # No web source, so the external branch never touches the network.
    monkeypatch.setattr(settings, "recipe_source", "off")
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _mock_forager_http(monkeypatch, handler):
    """Route recipes_forager's httpx client through a MockTransport."""
    mock = httpx.MockTransport(handler)
    monkeypatch.setattr(recipes_forager, "_client",
                        lambda timeout, transport=None: httpx.AsyncClient(
                            timeout=timeout, transport=mock))


def test_browse_includes_forager_when_active(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_secret")
    monkeypatch.setattr(settings, "forager_recipes_enabled", True)

    def handler(request):
        return httpx.Response(200, json={"recipes": [
            {"id": 5, "title": "Community Stew", "attribution": "Lee",
             "average_rating": 4.0, "rating_count": 3}]})

    _mock_forager_http(monkeypatch, handler)
    r = client.get("/mealie/recipes", params={"mine": False, "external": True, "search": "stew"})
    assert r.status_code == 200
    rows = r.json()
    assert any(x["source"] == "forager" and x["name"] == "Community Stew" for x in rows)


def test_browse_omits_forager_when_unlinked(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "")

    def handler(request):
        raise AssertionError("cloud must not be hit when unlinked")

    _mock_forager_http(monkeypatch, handler)
    r = client.get("/mealie/recipes", params={"mine": False, "external": True, "search": "stew"})
    assert r.status_code == 200
    assert r.json() == []


def test_browse_omits_forager_when_cloud_fails(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_secret")

    def handler(request):
        return httpx.Response(503, json={"detail": "down"})

    _mock_forager_http(monkeypatch, handler)
    r = client.get("/mealie/recipes", params={"mine": False, "external": True, "search": "stew"})
    assert r.status_code == 200
    assert r.json() == []


def test_share_posts_payload_and_confirms(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_secret")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read()
        return httpx.Response(201, json={"id": "xyz"})

    _mock_forager_http(monkeypatch, handler)
    r = client.post("/mealie/recipes/share", json={
        "name": "My Loaf", "ingredients": ["flour"], "instructions": ["bake"],
        "attribution": "Dan"})
    assert r.status_code == 200
    assert r.json()["id"] == "xyz"
    assert b"My Loaf" in seen["body"] and b"Dan" in seen["body"]


def test_share_uploads_image_bytes_and_surfaces_url(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_secret")
    # The recipe has a stored photo: the router should upload its bytes after
    # the submit succeeds, and pass the canonical URL back to the user.
    from app.routers import mealie as mealie_router

    async def fake_image_bytes(db, slug, native, mealie_id=None):
        return (b"\x89PNG\r\n\x1a\n photo", "image/png")
    monkeypatch.setattr(mealie_router, "_share_image_bytes", fake_image_bytes)

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/v1/recipes":
            return httpx.Response(201, json={
                "id": 7, "slug": "my-loaf", "share_token": "abc123",
                "url": "https://pantryraider.app/r/my-loaf-abc123"})
        if request.url.path == "/v1/recipes/7/image":
            return httpx.Response(200, json={
                "image_url": "https://forager.pantryraider.app/v1/recipes/7/image"})
        raise AssertionError(f"unexpected path {request.url.path}")

    _mock_forager_http(monkeypatch, handler)
    r = client.post("/mealie/recipes/share", json={
        "name": "My Loaf", "ingredients": ["flour"], "instructions": ["bake"],
        "attribution": "Dan"})
    assert r.status_code == 200
    # Both the recipe and its photo were sent.
    assert ("POST", "/v1/recipes") in calls
    assert ("POST", "/v1/recipes/7/image") in calls
    # The user gets the readable canonical link back.
    assert r.json()["url"] == "https://pantryraider.app/r/my-loaf-abc123"
    assert "pantryraider.app/r/my-loaf-abc123" in r.json()["message"]


def test_share_without_photo_sends_no_image_upload(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_secret")
    # No stored photo (the default helper finds nothing): only the submit is
    # sent, and the share still succeeds.
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(201, json={"id": 9})

    _mock_forager_http(monkeypatch, handler)
    r = client.post("/mealie/recipes/share", json={
        "name": "Plain Loaf", "ingredients": ["flour"], "instructions": ["bake"],
        "attribution": "Dan"})
    assert r.status_code == 200
    assert calls == ["/v1/recipes"]  # no image upload attempted


def test_share_requires_attribution(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_secret")

    def handler(request):  # never reached: builder rejects first
        raise AssertionError("must not post without attribution")

    _mock_forager_http(monkeypatch, handler)
    r = client.post("/mealie/recipes/share", json={
        "name": "My Loaf", "ingredients": ["flour"], "instructions": ["bake"],
        "attribution": ""})
    assert r.status_code == 422
    assert "credit" in r.json()["detail"].lower()


def test_share_prompts_when_not_linked(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "")
    r = client.post("/mealie/recipes/share", json={
        "name": "My Loaf", "ingredients": ["flour"], "attribution": "Dan"})
    assert r.status_code == 400
    assert "forager" in r.json()["detail"].lower()


def test_share_surfaces_rate_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_secret")

    def handler(request):
        return httpx.Response(429, json={"detail": "slow down"})

    _mock_forager_http(monkeypatch, handler)
    r = client.post("/mealie/recipes/share", json={
        "name": "My Loaf", "ingredients": ["flour"], "attribution": "Dan"})
    assert r.status_code == 429
    assert "minute" in r.json()["detail"].lower()


# --- router: bundle a set of community recipes into the library -------------

def _mock_mealie_http(monkeypatch, handler):
    """Route the Mealie client's shared httpx.AsyncClient through a MockTransport."""
    from app.services import mealie as mealie_mod
    monkeypatch.setattr(mealie_mod, "_client",
                        httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.fixture
def mealie_ready(monkeypatch):
    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie.test")
    monkeypatch.setattr(settings, "mealie_api_key", "mk_secret")


def test_bundle_adds_dedupes_and_counts_failures(client, monkeypatch, mealie_ready):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_secret")
    monkeypatch.setattr(settings, "forager_recipes_enabled", True)

    # Forager: a list of three community cards, then per-recipe details.
    def forager_handler(request):
        path = request.url.path
        if path == "/v1/recipes":
            return httpx.Response(200, json={"recipes": [
                {"id": 1, "title": "Alpha", "attribution": "Ana"},
                {"id": 2, "title": "Beta", "attribution": "Ben"},
                {"id": 3, "title": "Existing Stew", "attribution": "Cy"},
            ]})
        if path == "/v1/recipes/1":
            return httpx.Response(200, json={"id": 1, "title": "Alpha",
                                             "ingredients": ["a"], "steps": ["mix"]})
        if path == "/v1/recipes/2":
            return httpx.Response(200, json={"id": 2, "title": "Beta",
                                             "ingredients": ["b"], "steps": ["mix"]})
        raise AssertionError(f"unexpected forager path {path}")

    _mock_forager_http(monkeypatch, forager_handler)

    # Mealie: "Existing Stew" is already in the library (dedupe skips it); the
    # POST that creates "Beta" fails (counted, does not abort the batch).
    def mealie_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/recipes":
            return httpx.Response(200, json={"items": [
                {"name": "Existing Stew", "slug": "existing-stew"}]})
        if request.method == "POST" and path == "/api/recipes":
            name = (request.read() or b"").decode()
            if "Beta" in name:
                return httpx.Response(500, text="boom")
            return httpx.Response(201, json="alpha")
        if request.method == "PATCH" and path.startswith("/api/recipes/"):
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected mealie {request.method} {path}")

    _mock_mealie_http(monkeypatch, mealie_handler)

    r = client.post("/mealie/recipes/bundle-community", json={})
    assert r.status_code == 200
    d = r.json()
    assert d["added"] == 1 and d["skipped"] == 1 and d["failed"] == 1
    assert d["message"] == "Added 1 recipe, skipped 1 already in your library, 1 could not be added this time."


def test_bundle_prompts_when_not_linked(client, monkeypatch, mealie_ready):
    monkeypatch.setattr(settings, "cloud_instance_token", "")
    r = client.post("/mealie/recipes/bundle-community", json={})
    assert r.status_code == 400
    assert "forager" in r.json()["detail"].lower()


def test_bundle_requires_mealie(client, monkeypatch):
    # Only the Mealie backend needs Mealie configured; the native store
    # (the default with no Mealie) saves community recipes locally instead.
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_secret")
    monkeypatch.setattr(settings, "forager_recipes_enabled", True)
    monkeypatch.setattr(settings, "recipes_backend", "mealie")
    monkeypatch.setattr(settings, "mealie_base_url", "")
    monkeypatch.setattr(settings, "mealie_api_key", "")
    r = client.post("/mealie/recipes/bundle-community", json={})
    assert r.status_code == 400
    assert "mealie" in r.json()["detail"].lower()
