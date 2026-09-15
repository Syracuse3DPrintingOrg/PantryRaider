"""Shopping list "what you already own" (FoodAssistant-x1tt).

The Cook / shopping view marks each ingredient you already have, reusing the
same matcher the suggestion ranker uses: an ingredient is owned when it is in
Grocy stock OR on your staples list; only the rest is a shopping list. These
pins cover the pure partition helper and the Cook page render.
"""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services.mealie import partition_recipe_ingredients, reset_staple_cache
from app.config import settings

_SERVICE_DIR = Path(__file__).parent.parent / "service"


@pytest.fixture(autouse=True)
def fresh_staples(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "staple_items", "")
    reset_staple_cache()
    yield
    reset_staple_cache()


def ings(*names):
    return [{"note": n} for n in names]


def stock(*names):
    return [{"name": n} for n in names]


def test_in_stock_ingredient_is_owned():
    out = partition_recipe_ingredients(
        ings("chicken breast", "curry paste"), stock("Chicken Breast"))
    assert out["owned"] == ["chicken breast"]
    assert out["needed"] == ["curry paste"]


def test_staple_is_treated_as_owned():
    # Salt is a built-in staple: nothing in Grocy stock, but it still counts as
    # owned and never lands on the shopping list.
    out = partition_recipe_ingredients(ings("salt", "saffron"), stock())
    assert "salt" in out["owned"]
    assert out["needed"] == ["saffron"]


def test_custom_staple_list_counts_as_owned():
    settings.staple_items = "wasabi, nori"
    reset_staple_cache()
    out = partition_recipe_ingredients(ings("wasabi", "eel"), stock())
    assert "wasabi" in out["owned"]
    assert out["needed"] == ["eel"]


def test_water_is_neither_owned_nor_needed():
    out = partition_recipe_ingredients(
        ings("boiling water", "quinoa"), stock("Quinoa"))
    assert out["owned"] == ["quinoa"]
    assert out["needed"] == []


def test_nothing_needed_when_all_owned():
    out = partition_recipe_ingredients(
        ings("rice", "salt"), stock("Rice"))
    assert out["needed"] == []


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    cwd = os.getcwd()
    os.chdir(_SERVICE_DIR)
    try:
        from app.config import settings as s

        data_dir = tmp_path_factory.mktemp("data")
        s.data_dir = str(data_dir)

        from app.main import app

        s.grocy_base_url = "http://grocy.test"
        s.grocy_api_key = "test-grocy-key"
        s.vision_provider = "gemini"
        s.gemini_api_key = "test-gemini-key"
        s.mealie_base_url = "http://mealie.test"
        s.mealie_api_key = "test-mealie-key"
        s.auth_required = False
        s.auth_password = ""

        with TestClient(app) as c:
            yield c
    finally:
        os.chdir(cwd)


def test_cook_page_shows_owned_chips_and_staples_link(client):
    page = client.get("/ui/cook").text
    # Owned ingredients carry an "In stock" (Grocy) or "On hand" (staples) chip.
    assert "In stock" in page
    assert "On hand" in page
    # Ones you still need form the buy list.
    assert "Shopping list" in page
    # A link drops into the staples settings pane so you can say what you always
    # keep on hand.
    assert "setup#pane-recipe-tuning" in page
