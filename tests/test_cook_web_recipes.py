"""The Cook page ("What Can I Cook") surfaces web recipes, not just local ones.

Regression guard for the bug where web (TheMealDB / Spoonacular) results were
fetched but never shown: they were merged into the shared cookability tiers and
then squeezed out by the top-per-tier slice whenever the local Mealie library
was large, while the page's dedicated "From the Web" section was fed an
always-empty ``external_tiers`` dict.

The fix classifies each source into its own tier set so web recipes reach the
page's web section regardless of how many local recipes match. These tests hit
the /mealie/suggest route with a stubbed Mealie library, Grocy stock, and web
source, and check both the wiring (the web source is called) and the shape (web
results land in external_tiers, local in tiers).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import mealie as mealie_svc  # noqa: E402
from app.services import recipes_external  # noqa: E402
from app.services.grocy import GrocyClient  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie.test", raising=False)
    monkeypatch.setattr(settings, "mealie_api_key", "token", raising=False)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True,
                        raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


# A perishable stock item so a matching web recipe lands in a real tier
# (a matched, perishable ingredient plus extras -> "Worth a Shop Run").
_STOCK = [{"name": "Chicken Breast", "days_remaining": 2,
           "storage_bucket": "refrigerated"}]

# A local Mealie library big enough that a shared top-per-tier slice would keep
# only local recipes: every one is fully in stock ("Ready to Cook").
_LOCAL = [
    {"name": f"Local Chicken Dish {n}",
     "slug": f"local-{n}",
     "recipeIngredient": [{"note": "chicken breast"}]}
    for n in range(20)
]

# One web recipe that uses the perishable stock item plus a shop-run extra.
_WEB = [{
    "name": "Web Chicken Curry",
    "slug": None,
    "external_id": "52940",
    "source": "themealdb",
    "description": "Indian",
    "image": "https://example.test/curry.jpg",
    "source_url": "https://www.themealdb.com/meal/52940",
    "ingredients": ["1 Chicken Breast", "2 tbsp Curry Paste"],
    "recipeIngredient": [{"note": "1 Chicken Breast"}, {"note": "2 tbsp Curry Paste"}],
}]


def _suggest(client):
    async def fake_recipes(self):
        return _LOCAL

    async def fake_stock(self):
        return _STOCK

    async def fake_web(*args, **kwargs):
        return list(_WEB)

    with patch.object(mealie_svc.MealieClient, "get_recipes_with_ingredients",
                      fake_recipes), \
         patch.object(GrocyClient, "get_full_stock", fake_stock), \
         patch.object(recipes_external, "find_recipes_for_ingredients", fake_web):
        return client.get("/mealie/suggest")


def test_web_recipes_reach_the_page(client):
    r = _suggest(client)
    assert r.status_code == 200
    data = r.json()
    # The web source is counted...
    assert data["external_considered"] == 1
    # ...and its result is carried in the dedicated web tier set, not dropped.
    ext = data["external_tiers"]
    web_names = {s["name"] for tier in ext.values() for s in tier}
    assert "Web Chicken Curry" in web_names


def test_local_library_does_not_squeeze_out_web(client):
    # Even with a full local library, the web result still surfaces because the
    # two sources are tiered separately (the bug merged them and sliced).
    r = _suggest(client)
    data = r.json()
    local_names = {s["name"] for tier in data["tiers"].values() for s in tier}
    web_names = {s["name"] for tier in data["external_tiers"].values() for s in tier}
    assert local_names, "expected local recipes in the main tiers"
    assert "Web Chicken Curry" in web_names
    # Local and web stay in their own sections (no cross-contamination).
    assert "Web Chicken Curry" not in local_names


def test_web_disabled_leaves_web_tiers_empty(client):
    async def fake_recipes(self):
        return _LOCAL

    async def fake_stock(self):
        return _STOCK

    with patch.object(mealie_svc.MealieClient, "get_recipes_with_ingredients",
                      fake_recipes), \
         patch.object(GrocyClient, "get_full_stock", fake_stock):
        r = client.get("/mealie/suggest?external=false")
    assert r.status_code == 200
    data = r.json()
    assert data["external_considered"] == 0
    assert all(not tier for tier in data["external_tiers"].values())


def test_add_items_puts_web_buy_list_on_the_shopping_list(client):
    # The Cook popup's "Add to cart" for a web recipe posts its buy list here,
    # so ingredients reach the shopping list without saving the recipe first.
    added = []

    async def fake_lists(self):
        return [{"id": "list-1", "name": "Groceries"}]

    async def fake_add(self, list_id, item):
        added.append((list_id, item))
        return {"id": "x"}

    with patch.object(mealie_svc.MealieClient, "get_shopping_lists", fake_lists), \
         patch.object(mealie_svc.MealieClient, "add_shopping_item", fake_add):
        r = client.post("/mealie/shopping/add-items",
                        json={"items": ["Curry Paste", " ", "Coconut Milk"]})
    assert r.status_code == 200
    data = r.json()
    assert data["added"] == 2
    assert "Groceries" in data["message"]
    # Blank lines are dropped; the rest land on the first list.
    assert added == [("list-1", "Curry Paste"), ("list-1", "Coconut Milk")]


def test_add_items_empty_is_a_no_op(client):
    r = client.post("/mealie/shopping/add-items", json={"items": []})
    assert r.status_code == 200
    assert r.json()["added"] == 0


def test_cook_page_ships_in_app_quick_view(client):
    # The Cook page carries the popup and its Cook / Add to cart / Open buttons,
    # and the card title opens the popup rather than linking straight to Mealie.
    r = client.get("/ui/cook")
    assert r.status_code == 200
    body = r.text
    assert 'id="cookPreviewModal"' in body
    assert "openCookPreview(" in body
    assert 'id="cpv-cook"' in body and 'id="cpv-cart"' in body and 'id="cpv-open"' in body


def test_themealdb_is_the_free_default_web_source(monkeypatch):
    # No key, default source: the always-available free source is TheMealDB, so
    # a stock-based search must route to the mealdb path (never require a key).
    monkeypatch.setattr(settings, "recipe_source", "themealdb", raising=False)
    monkeypatch.setattr(settings, "spoonacular_api_key", "", raising=False)
    calls = {}

    async def fake_mealdb(ingredients, limit):
        calls["mealdb"] = list(ingredients)
        return list(_WEB)

    import asyncio
    with patch.object(recipes_external, "_mealdb_find", fake_mealdb):
        out = asyncio.run(
            recipes_external.find_recipes_for_ingredients(["Chicken Breast"]))
    assert calls.get("mealdb") == ["Chicken Breast"]
    assert out and out[0]["source"] == "themealdb"
