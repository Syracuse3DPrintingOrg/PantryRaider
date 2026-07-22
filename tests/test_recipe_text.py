"""Readable plain-text recipe rendering (FoodAssistant-74c1).

Covers the pure formatter (services/recipe_text.py) byte for byte: field
order, ingredient section headings, numbered steps, the 78-column wrap with
hanging indents, and graceful holes where a recipe has no description, times,
or source. The router surface (/mealie/recipes/export-text) is checked too:
plain-text media type, a .txt attachment filename, and the same formatter
output the download and the share email both read.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

from app.config import settings  # noqa: E402
from app.services import recipe_store  # noqa: E402
from app.services.recipe_text import format_recipe_text  # noqa: E402


# --- the formatter, byte for byte ---------------------------------------------

def test_full_recipe_renders_byte_exact():
    recipe = {
        "name": "Garlic Bread",
        "description": "Crusty and quick",
        "servings": "4 servings",
        "prep_time": "10 minutes",
        "cook_time": "15 minutes",
        "total_time": "25 minutes",
        "ingredients": [
            "1 loaf bread",
            "3 cloves garlic",
            "4 tablespoons butter, softened at room temperature so it spreads"
            " without tearing the crumb",
        ],
        "ingredient_sections": ["", "", ""],
        "instructions": [
            "Mix the butter and garlic.",
            "Spread the garlic butter over both halves of the loaf, working it"
            " into every corner so no bite is left dry, then close the halves"
            " back together.",
        ],
        "source_url": "https://example.com/garlic-bread",
    }
    expected = (
        "Garlic Bread\n"
        "\n"
        "Serves: 4 servings\n"
        "Prep time: 10 minutes\n"
        "Cook time: 15 minutes\n"
        "Total time: 25 minutes\n"
        "\n"
        "Crusty and quick\n"
        "\n"
        "INGREDIENTS\n"
        "\n"
        "- 1 loaf bread\n"
        "- 3 cloves garlic\n"
        "- 4 tablespoons butter, softened at room temperature so it spreads without\n"
        "  tearing the crumb\n"
        "\n"
        "STEPS\n"
        "\n"
        "1. Mix the butter and garlic.\n"
        "2. Spread the garlic butter over both halves of the loaf, working it into\n"
        "   every corner so no bite is left dry, then close the halves back together.\n"
        "\n"
        "Source: https://example.com/garlic-bread\n"
    )
    assert format_recipe_text(recipe) == expected


def test_section_headings_are_kept():
    recipe = {
        "name": "Deep Dish Pizza",
        "servings": "8 slices",
        "ingredients": ["3 cups flour", "1 cup warm water",
                        "2 cups mozzarella", "1 can crushed tomatoes"],
        "ingredient_sections": ["For the dough", "For the dough",
                                "For the topping", "For the topping"],
        "instructions": ["Make the dough.", "Top and bake."],
    }
    expected = (
        "Deep Dish Pizza\n"
        "\n"
        "Serves: 8 slices\n"
        "\n"
        "INGREDIENTS\n"
        "\n"
        "For the dough:\n"
        "- 3 cups flour\n"
        "- 1 cup warm water\n"
        "\n"
        "For the topping:\n"
        "- 2 cups mozzarella\n"
        "- 1 can crushed tomatoes\n"
        "\n"
        "STEPS\n"
        "\n"
        "1. Make the dough.\n"
        "2. Top and bake.\n"
    )
    assert format_recipe_text(recipe) == expected


def test_heading_mid_list_gets_a_separating_blank_line():
    # Unsectioned lines first, then a heading: the heading still stands apart.
    recipe = {
        "name": "Chili",
        "ingredients": ["1 lb beans", "1 onion", "sour cream", "chives"],
        "ingredient_sections": ["", "", "To serve", "To serve"],
        "instructions": ["Simmer for an hour."],
    }
    text = format_recipe_text(recipe)
    assert "- 1 onion\n\nTo serve:\n- sour cream\n" in text


def test_two_digit_steps_hang_their_wrap_under_the_text():
    long_step = ("Rest the dough under a damp towel until it has doubled in"
                 " size and springs back slowly when poked with a floured"
                 " finger.")
    recipe = {"name": "Bread",
              "ingredients": ["flour"],
              "instructions": [f"Step number {n}." for n in range(1, 10)] + [long_step]}
    text = format_recipe_text(recipe)
    lines = text.splitlines()
    start = lines.index(
        "10. Rest the dough under a damp towel until it has doubled in size and springs")
    # The wrapped remainder sits under the step text, past the "10. " prefix.
    assert lines[start + 1] == "    back slowly when poked with a floured finger."
    assert all(len(line) <= 78 for line in lines)


def test_missing_fields_leave_no_blank_scaffolding():
    recipe = {"name": "Toast", "ingredients": ["bread"],
              "instructions": ["Toast the bread."]}
    expected = (
        "Toast\n"
        "\n"
        "INGREDIENTS\n"
        "\n"
        "- bread\n"
        "\n"
        "STEPS\n"
        "\n"
        "1. Toast the bread.\n"
    )
    assert format_recipe_text(recipe) == expected


def test_empty_recipe_still_yields_a_title():
    assert format_recipe_text({}) == "Recipe\n"


# --- the download endpoint -----------------------------------------------------

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
    monkeypatch.setattr(settings, "recipes_backend", "native")
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _native_detail() -> dict:
    """A native-store detail dict (the Mealie-shaped read recipe_store serves),
    with a sectioned ingredient list."""
    return {
        "id": None, "native_id": 5, "slug": "garlic-bread", "name": "Garlic Bread",
        "description": "Crusty and quick", "recipeYield": "4 servings",
        "totalTime": "25 minutes", "prepTime": "10 minutes", "cookTime": "15 minutes",
        "orgURL": "https://example.com/garlic-bread", "origin": "manual",
        "tags": [], "categories": [], "image": "/recipes/images/5",
        "recipeIngredient": [
            {"note": "1 loaf bread", "title": "For the loaf"},
            {"note": "3 cloves garlic", "title": "For the butter"},
            {"note": "4 tablespoons butter"},
        ],
        "recipeInstructions": [{"text": "Mix the butter and garlic."},
                               {"text": "Toast until golden."}],
    }


def test_export_text_serves_readable_plain_text(client, monkeypatch):
    monkeypatch.setattr(recipe_store, "detail", lambda db, slug: _native_detail())
    r = client.get("/mealie/recipes/export-text", params={"slug": "garlic-bread"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert 'filename="garlic-bread.txt"' in r.headers["content-disposition"]
    expected = (
        "Garlic Bread\n"
        "\n"
        "Serves: 4 servings\n"
        "Prep time: 10 minutes\n"
        "Cook time: 15 minutes\n"
        "Total time: 25 minutes\n"
        "\n"
        "Crusty and quick\n"
        "\n"
        "INGREDIENTS\n"
        "\n"
        "For the loaf:\n"
        "- 1 loaf bread\n"
        "\n"
        "For the butter:\n"
        "- 3 cloves garlic\n"
        "- 4 tablespoons butter\n"
        "\n"
        "STEPS\n"
        "\n"
        "1. Mix the butter and garlic.\n"
        "2. Toast until golden.\n"
        "\n"
        "Source: https://example.com/garlic-bread\n"
    )
    assert r.text == expected


def test_export_text_unknown_recipe_404s(client, monkeypatch):
    monkeypatch.setattr(recipe_store, "detail", lambda db, slug: None)
    r = client.get("/mealie/recipes/export-text", params={"slug": "nope"})
    assert r.status_code == 404
    r = client.get("/mealie/recipes/export-text")
    assert r.status_code == 400
