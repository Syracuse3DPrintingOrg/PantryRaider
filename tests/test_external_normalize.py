"""Normalization of branded/sized Grocy stock names into the canonical
ingredient terms external recipe catalogs (TheMealDB filter.php) expect.

TheMealDB's filter.php only matches its single-ingredient taxonomy, so
"Baby Spinach" must reduce to "spinach" before querying.
"""
import asyncio

import httpx
import pytest

from app.services import recipes_external
from app.services.recipes_external import _core_ingredient


@pytest.mark.parametrize("raw, expected", [
    ("Baby Spinach", "spinach"),
    ("Organic Whole Milk", "milk"),
    ("Chicken Breast 1lb", "chicken breast"),
    ("Boneless Skinless Chicken Thighs", "chicken thighs"),
    # already-clean names pass through unchanged
    ("spinach", "spinach"),
    ("chicken breast", "chicken breast"),
])
def test_core_ingredient(raw, expected):
    assert _core_ingredient(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    # The real specialty-pantry names from the live device (FoodAssistant-tdma):
    # each must reduce to a base ingredient TheMealDB's taxonomy can match.
    ("Pickled Red Onions with Peppers", "onions"),
    ("Shredded Swiss Cheese", "cheese"),
    ("Boneless Skinless Chicken Breast", "chicken breast"),
    # more cheese/prep variants
    ("Sharp Cheddar Cheese", "cheese"),
    ("Grated Parmesan Cheese", "cheese"),
    # a bare cheese variety keeps its noun (no "cheese" word to anchor on)
    ("Feta", "feta"),
])
def test_core_ingredient_specialty_names(raw, expected):
    assert _core_ingredient(raw) == expected


def test_strips_embedded_quantities_and_units():
    assert _core_ingredient("500g Ground Beef") == "beef"
    assert _core_ingredient("2 x 400ml Coconut Cream") == "coconut cream"


def test_empty_and_pure_noise():
    assert _core_ingredient("") == ""
    assert _core_ingredient("Large Organic") == ""


# --- head-noun fallback in _mealdb_find -------------------------------------

def _stub_mealdb(monkeypatch, hits: dict[str, list[str]]):
    """Route recipes_external's httpx client at a TheMealDB filter.php stub.

    ``hits`` maps a filter.php ``i=`` value (underscored, e.g. "chicken_breast",
    "cheese") to the meal ids it returns; anything else returns no meals. Also
    stubs lookup.php so ranked ids normalize into recipes.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/filter.php"):
            term = request.url.params.get("i", "")
            ids = hits.get(term, [])
            return httpx.Response(200, json={"meals": [{"idMeal": i} for i in ids] or None})
        if request.url.path.endswith("/lookup.php"):
            mid = request.url.params.get("i", "")
            return httpx.Response(200, json={"meals": [{
                "idMeal": mid, "strMeal": f"Meal {mid}",
                "strInstructions": "cook", "strMealThumb": "",
                "strIngredient1": "chicken", "strMeasure1": "1",
            }]})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    monkeypatch.setattr(recipes_external, "_client", client)
    # Isolate caches between runs.
    monkeypatch.setattr(recipes_external, "_search_cache", {})
    monkeypatch.setattr(recipes_external, "_recipe_cache", {})


def test_mealdb_find_falls_back_to_head_noun(monkeypatch):
    # TheMealDB only knows the base word "cheese", not "shredded swiss cheese",
    # and "onions", not "red onions". The specialty names must still yield hits.
    _stub_mealdb(monkeypatch, hits={"cheese": ["101"], "onions": ["202"]})
    recipes = asyncio.run(recipes_external._mealdb_find(
        ["Shredded Swiss Cheese", "Pickled Red Onions with Peppers"], limit=12))
    ids = {r["external_id"] for r in recipes}
    assert ids == {"101", "202"}


def test_mealdb_find_prefers_full_term_then_word(monkeypatch):
    # "chicken breast" reduces to a two-word core. If TheMealDB has no
    # "chicken_breast" entry, the head-noun retry ("breast") also misses, so it
    # falls through to the other word ("chicken"), which hits.
    _stub_mealdb(monkeypatch, hits={"chicken": ["55"]})
    recipes = asyncio.run(recipes_external._mealdb_find(
        ["Boneless Skinless Chicken Breast"], limit=12))
    assert {r["external_id"] for r in recipes} == {"55"}


def test_mealdb_find_specialty_pantry_yields_recipes(monkeypatch):
    # End-to-end for the bug: a pantry of only specialty items (the top ones
    # exotic) now returns recipes because reduction + fallback reach staples.
    _stub_mealdb(monkeypatch, hits={"cheese": ["1"], "chicken": ["2"]})
    stock = ["Utica Greens", "Pickled Red Onions with Peppers",
             "Shredded Swiss Cheese", "Boneless Skinless Chicken Breast"]
    recipes = asyncio.run(recipes_external._mealdb_find(stock, limit=12))
    assert {r["external_id"] for r in recipes} == {"1", "2"}
