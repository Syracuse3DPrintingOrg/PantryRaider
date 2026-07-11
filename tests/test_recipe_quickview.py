"""In-app quick view of a saved Mealie recipe (FoodAssistant-az1s).

A user can read a saved recipe's ingredients and steps without opening Mealie in
another tab. These tests cover:

  * GET /mealie/recipes/detail normalizes a Mealie recipe into the same shape the
    preview modal renders (ingredients + steps as display strings), and fails
    soft on a missing slug or when Mealie cannot be reached.
  * The Recipes page exposes the quick-view action on Mealie rows and the modal
    markup supports a read-only mode (Cook + Open in Mealie, no Save).

Mealie HTTP is mocked; no network.
"""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SERVICE_DIR = Path(__file__).parent.parent / "service"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    cwd = os.getcwd()
    os.chdir(_SERVICE_DIR)
    try:
        from app.config import settings

        data_dir = tmp_path_factory.mktemp("data")
        settings.data_dir = str(data_dir)

        from app.main import app

        settings.grocy_base_url = "http://grocy.test"
        settings.grocy_api_key = "test-grocy-key"
        settings.vision_provider = "gemini"
        settings.gemini_api_key = "test-gemini-key"
        settings.mealie_base_url = "http://mealie.test"
        settings.mealie_api_key = "test-mealie-key"
        settings.auth_required = False
        settings.auth_password = ""
        assert settings.mealie_configured()

        with TestClient(app) as c:
            yield c
    finally:
        os.chdir(cwd)


_SAMPLE_DETAIL = {
    "id": "abc-123",
    "name": "Weeknight Chili",
    "slug": "weeknight-chili",
    "description": "A quick pantry chili.",
    "recipeYield": "4 servings",
    "totalTime": "45 minutes",
    "recipeIngredient": [
        {"display": "2 cups kidney beans", "food": {"name": "kidney beans"}},
        {"quantity": 1, "unit": {"name": "tbsp"}, "food": {"name": "chili powder"}},
        {"note": "salt to taste"},
        {"food": {"name": ""}},  # empty entries are dropped
    ],
    "recipeInstructions": [
        {"text": "Brown the aromatics."},
        {"text": "Simmer everything together."},
        {"text": "   "},  # blank steps are dropped
    ],
}


def test_detail_returns_normalized_shape(client, monkeypatch):
    from app.routers import mealie as mealie_router

    async def fake_get_recipe(self, slug):
        assert slug == "weeknight-chili"
        return _SAMPLE_DETAIL

    monkeypatch.setattr(mealie_router.MealieClient, "get_recipe", fake_get_recipe)

    r = client.get("/mealie/recipes/detail", params={"slug": "weeknight-chili"})
    assert r.status_code == 200
    d = r.json()
    assert d["name"] == "Weeknight Chili"
    assert d["slug"] == "weeknight-chili"
    assert d["description"] == "A quick pantry chili."
    assert d["servings"] == "4 servings"
    assert d["total_time"] == "45 minutes"
    # Ingredients are display strings, composed when no display/note is present.
    assert d["ingredients"] == [
        "2 cups kidney beans",
        "1 tbsp chili powder",
        "salt to taste",
    ]
    # Steps are plain strings, blanks dropped.
    assert d["instructions"] == [
        "Brown the aromatics.",
        "Simmer everything together.",
    ]
    # Carries an Open-in-Mealie base URL for the modal link.
    assert d["mealie_url"]


def test_detail_missing_slug_fails_soft(client):
    r = client.get("/mealie/recipes/detail", params={"slug": "   "})
    assert r.status_code == 400
    assert "detail" in r.json()


def test_detail_mealie_down_fails_soft(client, monkeypatch):
    from app.routers import mealie as mealie_router

    async def boom(self, slug):
        raise mealie_router.MealieError("connection refused")

    monkeypatch.setattr(mealie_router.MealieClient, "get_recipe", boom)

    r = client.get("/mealie/recipes/detail", params={"slug": "weeknight-chili"})
    assert r.status_code == 502
    assert "Could not load" in r.json()["detail"]


def test_detail_not_found_fails_soft(client, monkeypatch):
    from app.routers import mealie as mealie_router

    async def none(self, slug):
        return {}

    monkeypatch.setattr(mealie_router.MealieClient, "get_recipe", none)

    r = client.get("/mealie/recipes/detail", params={"slug": "ghost"})
    assert r.status_code == 404


def test_recipes_page_has_mealie_quick_view(client):
    page = client.get("/ui/recipes").text
    # Mealie rows expose the in-app quick view via a View button and the name.
    assert "function openMealiePreview(" in page
    assert "openMealiePreview('${r.slug}'" in page
    assert "bi-eye me-1\"></i>View" in page
    # It fetches the new detail endpoint and cooks via the existing set-active flow.
    assert "mealie/recipes/detail" in page
    assert "cookFromMealiePreview(" in page
    assert "setCurrentFromMealie(mealiePreview.slug" in page


def test_quick_view_modal_supports_read_only_mode(client):
    page = client.get("/ui/recipes").text
    # The modal gains an Open-in-Mealie link, shown for saved recipes.
    assert 'id="pv-open-mealie"' in page
    assert "Open in Mealie" in page
    # Read-only mode hides Save; the community flow restores it.
    assert "document.getElementById('pv-save').classList.add('d-none')" in page
    assert "saveBtn.classList.remove('d-none')" in page
