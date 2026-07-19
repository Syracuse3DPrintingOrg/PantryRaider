"""Unit tests for the in-memory active-recipe holder (FoodAssistant-879b).

Pure-service tests need no app or network. A couple of endpoint tests reuse the
smoke-route client fixture.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "service"))

from app.services import current_recipe  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    current_recipe.clear_active()
    yield
    current_recipe.clear_active()


def test_get_active_empty_is_none():
    assert current_recipe.get_active() is None


def test_set_and_get_normalizes():
    out = current_recipe.set_active({
        "title": "  Soup  ",
        "source": "mealie",
        "id": 42,
        "servings": 4,
        "ingredients": [
            {"name": " Onion ", "quantity": "2", "unit": "ea"},
            "Salt",                       # bare string ingredient
            {"name": "", "quantity": 1},  # nameless dropped
        ],
        "steps": ["Chop", "  ", "Simmer"],
        "notes": "tasty",
    })
    assert out["title"] == "Soup"
    assert out["id"] == "42"
    assert out["servings"] == 4
    assert out["servings_scale"] == 1.0
    assert out["steps"] == ["Chop", "Simmer"]   # blank step dropped
    names = [i["name"] for i in out["ingredients"]]
    assert names == ["Onion", "Salt"]            # nameless dropped
    assert out["ingredients"][0]["quantity"] == 2.0
    assert out["ingredients"][0]["scaled_quantity"] == 2.0
    assert out["ingredients"][1]["quantity"] is None  # "Salt" has no qty
    assert current_recipe.get_active()["title"] == "Soup"


def test_unparseable_quantity_becomes_none():
    out = current_recipe.set_active({
        "title": "x",
        "ingredients": [{"name": "Pepper", "quantity": "to taste"}],
    })
    assert out["ingredients"][0]["quantity"] is None
    assert out["ingredients"][0]["scaled_quantity"] is None


def test_clear_active():
    current_recipe.set_active({"title": "x"})
    current_recipe.clear_active()
    assert current_recipe.get_active() is None


def test_scale_servings_math():
    current_recipe.set_active({
        "title": "Bread",
        "servings": 2,
        "ingredients": [{"name": "Flour", "quantity": 100, "unit": "g"}],
    })
    out = current_recipe.scale_servings(2.5)
    assert out["servings_scale"] == 2.5
    assert out["scaled_servings"] == 5.0
    # Base quantity is preserved; scaled is derived.
    assert out["ingredients"][0]["quantity"] == 100
    assert out["ingredients"][0]["scaled_quantity"] == 250.0

    # Scaling back down recovers the original, proving base is never mutated.
    back = current_recipe.scale_servings(1.0)
    assert back["ingredients"][0]["scaled_quantity"] == 100.0


def test_scale_ignores_non_positive_factor():
    current_recipe.set_active({"title": "x", "servings": 1})
    current_recipe.scale_servings(3)
    out = current_recipe.scale_servings(0)     # ignored
    assert out["servings_scale"] == 3
    out = current_recipe.scale_servings(-2)    # ignored
    assert out["servings_scale"] == 3


def test_scale_with_no_active_returns_none():
    assert current_recipe.scale_servings(2) is None


def test_servings_floor_is_one():
    out = current_recipe.set_active({"title": "x", "servings": 0})
    assert out["servings"] == 1


# -- Mealie detail -> active recipe (FoodAssistant-1g4l) --------------------


def test_from_mealie_detail_maps_structured_fields():
    detail = {
        "name": "Pasta Pomodoro",
        "slug": "pasta-pomodoro",
        "recipeYield": "4 servings",
        "description": "Quick weeknight pasta.",
        "recipeIngredient": [
            {"quantity": 2, "unit": {"name": "cup"}, "food": {"name": "pasta"}, "note": "dry"},
            {"note": "Salt to taste"},
        ],
        "recipeInstructions": [
            {"text": "Boil the pasta."},
            {"text": "Add the sauce."},
        ],
    }
    out = current_recipe.set_active(current_recipe.from_mealie_detail(detail, "pasta-pomodoro"))
    assert out["title"] == "Pasta Pomodoro"
    assert out["source"] == "mealie"
    assert out["id"] == "pasta-pomodoro"
    assert out["servings"] == 4
    # Structured ingredient keeps name/quantity/unit; unstructured one is name-only.
    assert out["ingredients"][0]["name"] == "pasta"
    assert out["ingredients"][0]["quantity"] == 2
    assert out["ingredients"][0]["unit"] == "cup"
    assert out["ingredients"][1]["name"] == "Salt to taste"
    assert out["steps"] == ["Boil the pasta.", "Add the sauce."]


def test_from_mealie_detail_carries_prep_cook_total_time_for_printing():
    # Feeds print_document.format_quick_facts (FoodAssistant-gm4c); "performTime"
    # is Mealie's raw field name for cook time.
    detail = {
        "name": "Pasta Pomodoro",
        "prepTime": "10 minutes",
        "performTime": "20 minutes",
        "totalTime": "30 minutes",
    }
    out = current_recipe.from_mealie_detail(detail, "pasta-pomodoro")
    assert out["prep_time"] == "10 minutes"
    assert out["cook_time"] == "20 minutes"
    assert out["total_time"] == "30 minutes"


def test_from_mealie_detail_tolerates_sparse_recipe():
    out = current_recipe.from_mealie_detail({"name": "Bare"}, "bare")
    assert out["title"] == "Bare"
    assert out["servings"] == 1
    assert out["ingredients"] == []
    assert out["steps"] == []


def test_mealie_servings_parsing():
    assert current_recipe._mealie_servings("4 servings") == 4
    assert current_recipe._mealie_servings(6) == 6
    assert current_recipe._mealie_servings("") == 1
    assert current_recipe._mealie_servings("serves a crowd") == 1
    assert current_recipe._mealie_servings(0) == 1
