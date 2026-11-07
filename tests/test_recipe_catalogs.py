"""Your own recipe databases (FoodAssistant-1sfs6).

A recipe catalog is a site serving the Pantry Raider Recipe Catalog API
(docs/recipe-catalogs.md). These tests run a mock catalog shaped like
Deeserved's through an httpx.MockTransport and check the whole path: the
contract parsing, adding and removing a catalog under /setup, catalog recipes
in search, in the Cook page's matched tiers, in preview, and saved into the
native library with their section headings and credit.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import recipe_catalogs, recipe_source  # noqa: E402
from app.services.grocy import GrocyClient  # noqa: E402

BASE = "https://family.test/api/pantry-raider"

_ZUPPA = {
    "id": "87", "slug": "zuppa-toscana", "name": "Zuppa Toscana",
    "description": "Sausage and kale soup.",
    "image": "https://family.test/media/zuppa.webp",
    "url": "https://family.test/recipes/zuppa-toscana",
    "servings": "4 to 6", "total_time": "Crock pot ~ 3 hrs",
    "categories": ["Soups and salads"], "cuisine": "", "author": "Liz",
    "updated_at": "2026-11-01T00:00:00Z",
    "ingredients": ["1 lb spicy sausage", "4 cups chopped kale", "6 cups chicken broth"],
}
_PIE = {
    "id": "12", "slug": "apple-pie", "name": "Apple Pie", "description": "",
    "image": None, "url": None, "servings": "", "total_time": "",
    "categories": ["Baking"], "cuisine": "", "author": "",
    "updated_at": "2026-11-01T00:00:00Z",
    "ingredients": ["6 apples", "1 pie crust"],
}
_DETAIL = {**_ZUPPA,
           "ingredient_groups": [
               {"heading": None, "items": ["1 lb spicy sausage", "4 cups chopped kale"]},
               {"heading": "For the broth", "items": ["6 cups chicken broth"]}],
           "instructions": ["Brown the sausage.", "Combine in the crock pot."],
           "instruction_groups": [{"heading": None, "items": ["Brown the sausage.",
                                                              "Combine in the crock pot."]}],
           "notes": ["Great on rainy days."]}


class MockCatalog:
    """A catalog API server: records requests, optionally demands a key."""

    def __init__(self, key: str = ""):
        self.key = key
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.key and request.headers.get("authorization") != f"Bearer {self.key}":
            return httpx.Response(401, json={"error": "Invalid or expired API key."})
        path = request.url.path
        if path == "/api/pantry-raider/catalog":
            return httpx.Response(200, json={
                "api": "pantry-raider-catalog", "version": 1, "name": "Family Box",
                "description": "", "url": "https://family.test", "recipe_count": 2,
                "updated_at": "2026-11-01T00:00:00Z"})
        if path == "/api/pantry-raider/recipes":
            rows = [_PIE, _ZUPPA]
            return httpx.Response(200, json={"total": 2, "offset": 0, "limit": 200,
                                             "recipes": rows})
        if path in ("/api/pantry-raider/recipes/87", "/api/pantry-raider/recipes/zuppa-toscana"):
            return httpx.Response(200, json=_DETAIL)
        if path == "/":
            return httpx.Response(200, text="<html>home page</html>")
        return httpx.Response(404, json={"error": "Recipe not found."})


@pytest.fixture
def catalog(monkeypatch):
    server = MockCatalog()
    monkeypatch.setattr(recipe_catalogs, "_transport", httpx.MockTransport(server))
    recipe_catalogs.forget_cache()
    yield server
    recipe_catalogs.forget_cache()


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "recipes_backend", "native", raising=False)
    monkeypatch.setattr(settings, "recipe_source", "off", raising=False)
    monkeypatch.setattr(settings, "recipe_catalogs", [], raising=False)
    monkeypatch.setattr(settings, "recipe_catalog_keys", {}, raising=False)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True, raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


# ── Pure contract handling ────────────────────────────────────────────────────

@pytest.mark.parametrize("pasted, base", [
    ("https://family.test/api/pantry-raider", BASE),
    ("https://family.test/api/pantry-raider/", BASE),
    ("https://family.test/api/pantry-raider/catalog", BASE),
    ("https://family.test/api/pantry-raider/recipes?q=x", BASE),
    ("family.test/api/pantry-raider", BASE),
])
def test_base_url_is_normalized(pasted, base):
    assert recipe_catalogs.normalize_base_url(pasted) == base


@pytest.mark.parametrize("bad", ["", "ftp://family.test/x", "javascript:alert(1)"])
def test_bad_addresses_are_refused(bad):
    with pytest.raises(recipe_catalogs.CatalogError):
        recipe_catalogs.normalize_base_url(bad)


def test_info_must_name_the_catalog_api():
    with pytest.raises(recipe_catalogs.CatalogError):
        recipe_catalogs.parse_info({"name": "Some JSON site"})
    with pytest.raises(recipe_catalogs.CatalogError):
        recipe_catalogs.parse_info({"api": "pantry-raider-catalog"})
    info = recipe_catalogs.parse_info({"api": "pantry-raider-catalog", "version": 1,
                                       "name": "Box", "recipe_count": "3"})
    assert info["name"] == "Box" and info["recipe_count"] == 3


def test_summary_matches_the_shared_external_shape():
    r = recipe_catalogs.normalize_summary(_ZUPPA, "catalog:box")
    assert r["source"] == "catalog:box" and r["external_id"] == "87"
    assert r["recipeIngredient"][0] == {"note": "1 lb spicy sausage"}
    assert "Soups and salads" in r["cuisine"]
    assert recipe_catalogs.normalize_summary({"id": "1"}, "catalog:box") is None
    assert recipe_catalogs.normalize_summary({"name": "No id"}, "catalog:box") is None
    # A non-http image never reaches the page.
    assert recipe_catalogs.normalize_summary(
        {**_ZUPPA, "image": "javascript:x"}, "catalog:box")["image"] == ""


def test_detail_keeps_section_headings_and_credit():
    r = recipe_catalogs.normalize_detail(_DETAIL, "catalog:box", "Family Box")
    assert r["ingredients"] == ["1 lb spicy sausage", "4 cups chopped kale",
                                "6 cups chicken broth"]
    assert r["ingredient_sections"] == [None, None, "For the broth"]
    assert r["instructions"] == ["Brown the sausage.", "Combine in the crock pot."]
    assert "From Liz, shared through Family Box." in r["description"]
    assert "Great on rainy days." in r["description"]


def test_ids_are_unique():
    assert recipe_catalogs.make_id("Family Box") == "family-box"
    assert recipe_catalogs.make_id("Family Box", {"family-box"}) == "family-box-2"
    assert recipe_catalogs.make_id("!!!") == "catalog"


def test_badge_names_the_catalog(monkeypatch):
    monkeypatch.setattr(settings, "recipe_catalogs",
                        [{"id": "box", "name": "Family Box", "url": BASE}], raising=False)
    assert recipe_source.source_badge("catalog:box")["label"] == "Family Box"
    assert recipe_source.source_badge("catalog:gone")["label"] == "Recipe database"


# ── Managing catalogs under /setup ────────────────────────────────────────────

def test_add_list_toggle_and_remove(client, catalog):
    r = client.get("/setup/recipe-catalogs")
    assert r.status_code == 200
    assert [p["id"] for p in r.json()["presets"]] == ["deeserved"]

    r = client.post("/setup/recipe-catalogs", json={"url": BASE + "/catalog"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["id"] == "family-box"
    assert d["catalogs"] == [{"id": "family-box", "name": "Family Box", "url": BASE,
                              "enabled": True, "has_key": False}]
    saved = json.loads((Path(settings.data_dir) / "settings.json").read_text())
    assert saved["recipe_catalogs"][0]["url"] == BASE

    assert client.post("/setup/recipe-catalogs", json={"url": BASE}).status_code == 409

    r = client.post("/setup/recipe-catalogs/family-box/enabled", json={"enabled": False})
    assert r.json()["catalogs"][0]["enabled"] is False
    assert recipe_catalogs.enabled() == []

    r = client.delete("/setup/recipe-catalogs/family-box")
    assert r.status_code == 200 and r.json()["catalogs"] == []
    assert client.delete("/setup/recipe-catalogs/family-box").status_code == 404


def test_a_site_that_is_not_a_catalog_is_not_added(client, catalog):
    r = client.post("/setup/recipe-catalogs", json={"url": "https://family.test/"})
    assert r.status_code == 422
    assert recipe_catalogs.configured() == []
    t = client.post("/setup/recipe-catalogs/test", json={"url": "https://family.test/nope"})
    assert t.json()["ok"] is False


def test_access_key_is_sent_stored_apart_and_never_listed(client, monkeypatch):
    server = MockCatalog(key="s3cret")
    monkeypatch.setattr(recipe_catalogs, "_transport", httpx.MockTransport(server))
    recipe_catalogs.forget_cache()
    assert client.post("/setup/recipe-catalogs/test", json={"url": BASE}).json()["ok"] is False
    r = client.post("/setup/recipe-catalogs", json={"url": BASE, "api_key": "s3cret"})
    assert r.status_code == 200, r.text
    assert "s3cret" not in r.text
    assert r.json()["catalogs"][0]["has_key"] is True
    assert settings.recipe_catalog_keys == {"family-box": "s3cret"}
    # An edit with the key left blank keeps it.
    r = client.post("/setup/recipe-catalogs", json={"id": "family-box", "url": BASE,
                                                    "name": "Grandma"})
    assert r.status_code == 200 and r.json()["catalogs"][0]["name"] == "Grandma"
    assert settings.recipe_catalog_keys == {"family-box": "s3cret"}
    recipe_catalogs.forget_cache()


def test_catalog_keys_are_a_secret_setting():
    from app import config
    assert "recipe_catalog_keys" in config.SECRET_SETTING_KEYS
    assert "recipe_catalogs" not in config.SECRET_SETTING_KEYS
    assert "recipe_catalog_keys" not in config.SATELLITE_PULL_FIELDS


# ── Catalog recipes across the app ────────────────────────────────────────────

def _add(client):
    assert client.post("/setup/recipe-catalogs", json={"url": BASE}).status_code == 200


def test_search_browses_and_filters_catalog_recipes(client, catalog):
    _add(client)
    rows = client.get("/mealie/recipes?external=true&mine=false").json()
    names = [r["name"] for r in rows]
    assert names == ["Apple Pie", "Zuppa Toscana"]
    assert rows[1]["source"] == "catalog:family-box"
    assert rows[1]["badge"]["label"] == "Family Box"
    rows = client.get("/mealie/recipes?external=true&mine=false&search=kale").json()
    assert [r["name"] for r in rows] == ["Zuppa Toscana"]
    # Off by default: the "Other recipes" toggle gates them like the web sources.
    assert client.get("/mealie/recipes?mine=false").json() == []
    # The listing is cached: three searches, one listing call.
    assert sum(1 for c in catalog.calls if c.url.path.endswith("/recipes")) == 1


def test_cook_page_matches_catalog_recipes_against_stock(client, catalog):
    _add(client)

    async def fake_stock(self):
        return [{"name": "Kale", "days_remaining": 2, "storage_bucket": "refrigerated"},
                {"name": "Spicy Sausage", "days_remaining": 3, "storage_bucket": "refrigerated"}]

    async def no_units(grocy):
        return None, None

    with patch.object(GrocyClient, "get_full_stock", fake_stock), \
         patch("app.routers.mealie._unit_tables", no_units):
        data = client.get("/mealie/suggest").json()
    ext = [s for tier in data["external_tiers"].values() for s in tier]
    zuppa = next(s for s in ext if s["name"] == "Zuppa Toscana")
    assert zuppa["source"] == "catalog:family-box"
    assert zuppa["badge"]["label"] == "Family Box"
    assert len(zuppa["matched_ingredients"]) >= 2
    # Matching is local: no stock name ever went to the catalog.
    for call in catalog.calls:
        assert "kale" not in str(call.url).lower() and "sausage" not in str(call.url).lower()


def test_preview_and_save_into_the_library(client, catalog):
    _add(client)
    d = client.get("/mealie/recipes/external-detail",
                   params={"external_id": "87", "source": "catalog:family-box"})
    assert d.status_code == 200
    assert d.json()["instructions"] == ["Brown the sausage.", "Combine in the crock pot."]

    async def no_image(url):
        return None

    with patch("app.services.recipe_store.fetch_image", no_image):
        r = client.post("/mealie/recipes/import-external",
                        json={"external_id": "87", "source": "catalog:family-box"})
    assert r.status_code == 200, r.text
    slug = r.json()["slug"]
    detail = client.get("/mealie/recipes/detail", params={"slug": slug}).json()
    assert detail["name"] == "Zuppa Toscana"
    assert "shared through Family Box" in (detail.get("description") or "")

    missing = client.get("/mealie/recipes/external-detail",
                         params={"external_id": "999", "source": "catalog:family-box"})
    assert missing.status_code == 404


def test_a_catalog_that_is_down_adds_nothing(client, catalog, monkeypatch):
    _add(client)
    recipe_catalogs.forget_cache()

    def down(request):
        raise httpx.ConnectError("down", request=request)

    monkeypatch.setattr(recipe_catalogs, "_transport", httpx.MockTransport(down))
    r = client.get("/mealie/recipes?external=true&mine=false")
    assert r.status_code == 200 and r.json() == []
