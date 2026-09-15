"""Open in Mealie on the On the Line page (FoodAssistant-f546).

The link is rendered client-side from the current recipe's source/slug, so these
tests cover the two halves that make it work:

  * the /current-recipe/all data carries source == "mealie" and the slug for a
    Mealie recipe, and does not for a non-Mealie one (the data the link needs);
  * the page ships the Open in Mealie anchor wired to the configured Mealie URL
    and gated on a Mealie source.

No network: the current recipe is set directly through the in-memory holder.
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
        settings.data_dir = str(tmp_path_factory.mktemp("data"))
        from app.main import app
        settings.grocy_base_url = "http://grocy.test"
        settings.grocy_api_key = "test-grocy-key"
        settings.mealie_base_url = "http://mealie.test:9285"
        settings.mealie_api_key = "test-mealie-key"
        settings.mealie_public_url = "https://recipes.example.com"
        settings.auth_required = False
        settings.auth_password = ""
        with TestClient(app) as c:
            yield c
    finally:
        os.chdir(cwd)


@pytest.fixture(autouse=True)
def _clean_recipe():
    from app.services import current_recipe
    current_recipe.clear_active()
    yield
    current_recipe.clear_active()


def test_all_carries_source_and_slug_for_mealie_recipe(client):
    from app.services import current_recipe
    current_recipe.set_active({"title": "Pasta", "source": "mealie", "id": "pasta-pomodoro"})
    r = client.get("/current-recipe/all")
    assert r.status_code == 200, r.text
    recipe = r.json()["recipes"][0]
    assert recipe["source"] == "mealie"
    assert recipe["id"] == "pasta-pomodoro"


def test_all_non_mealie_recipe_is_not_flagged(client):
    from app.services import current_recipe
    current_recipe.set_active({"title": "Freehand Soup", "source": "ai"})
    r = client.get("/current-recipe/all")
    recipe = r.json()["recipes"][0]
    assert recipe["source"] != "mealie"


def test_page_ships_open_in_mealie_anchor_and_url(client):
    r = client.get("/ui/current-recipe")
    assert r.status_code == 200
    html = r.text
    # The anchor exists and the Mealie link URL is wired into the page.
    assert 'id="cr-open-mealie"' in html
    assert "recipes.example.com" in html
    # Gated on a Mealie source with the shared /g/home/r/ link shape.
    assert "r.source === 'mealie'" in html
    assert "/g/home/r/" in html
