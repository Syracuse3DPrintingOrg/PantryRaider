"""The native meal plan (FoodAssistant-g0fd).

The Meal Plan page, the deck's today-meal key, and the HA summary all speak
the /mealie/mealplan wire shapes; on the native recipe backend those now come
from Pantry Raider's own MealPlanEntry table. Covers CRUD through the
endpoints with NO Mealie configured, recipe entries picked by the ids the
recipe search returns, the free-text fallback, the HA summary shape, and the
recipe-printing seam.
"""
import os
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SERVICE_DIR = Path(__file__).parent.parent / "service"

_TAG = uuid.uuid4().hex[:8]


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
        settings.mealie_base_url = ""
        settings.mealie_api_key = ""
        settings.recipes_backend = ""
        settings.auth_required = False
        settings.auth_password = ""

        with TestClient(app) as c:
            yield c
    finally:
        os.chdir(cwd)


def _today() -> str:
    return date.today().isoformat()


def test_mealplan_crud_native(client):
    # A saved recipe to plan, picked by the id the recipe search returns.
    name = f"Plan Stew {_TAG}"
    slug = client.post("/mealie/recipes/create", json={
        "name": name, "ingredients": ["beef"], "instructions": ["Stew it."],
    }).json()["slug"]
    try:
        listing = client.get("/mealie/recipes", params={"search": name}).json()
        recipe_id = next(r["id"] for r in listing if r["slug"] == slug)

        r = client.post("/mealie/mealplan", json={
            "date": _today(), "entry_type": "dinner",
            "recipe_id": str(recipe_id), "title": "",
        })
        assert r.status_code == 200
        entry_id = r.json()["id"]

        r = client.post("/mealie/mealplan", json={
            "date": _today(), "entry_type": "lunch", "title": "Leftovers",
        })
        assert r.status_code == 200
        free_id = r.json()["id"]

        data = client.get("/mealie/mealplan", params={"days": 7}).json()
        assert data["mealie_url"] is None
        today_entries = data["days"][_today()]
        by_id = {e["id"]: e for e in today_entries}
        assert by_id[entry_id]["title"] == name
        assert by_id[entry_id]["recipe_slug"] == slug
        assert by_id[entry_id]["entry_type"] == "dinner"
        assert by_id[free_id]["title"] == "Leftovers"
        assert by_id[free_id]["recipe_slug"] is None
        # Every day in the window is present, entries or not.
        assert len(data["days"]) == 7

        s = client.get("/mealie/mealplan/summary").json()
        assert s["count"] == 2
        assert {e["name"] for e in s["today"]} == {name, "Leftovers"}
        assert s["tomorrow"] == []

        for eid in (entry_id, free_id):
            assert client.delete(f"/mealie/mealplan/{eid}").status_code == 200
        assert client.delete(f"/mealie/mealplan/{entry_id}").status_code == 404
        assert client.get("/mealie/mealplan/summary").json()["count"] == 0
    finally:
        client.delete(f"/recipes/{slug}")


def test_mealplan_validation_native(client):
    r = client.post("/mealie/mealplan", json={"date": _today(), "title": ""})
    assert r.status_code == 400
    r = client.post("/mealie/mealplan", json={
        "date": _today(), "recipe_id": "999999", "title": "",
    })
    assert r.status_code == 400
    assert "could not be found" in r.json()["detail"]


def test_mealplan_tomorrow_in_summary(client):
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    eid = client.post("/mealie/mealplan", json={
        "date": tomorrow, "entry_type": "breakfast", "title": f"Pancakes {_TAG}",
    }).json()["id"]
    try:
        s = client.get("/mealie/mealplan/summary").json()
        assert s["count"] == 0
        assert s["tomorrow"] == [{"type": "breakfast", "name": f"Pancakes {_TAG}"}]
    finally:
        client.delete(f"/mealie/mealplan/{eid}")


@pytest.mark.anyio
async def test_printing_recipe_fetch_native(client):
    """The document-print recipe fetch reads the native store in native mode."""
    name = f"Print Pie {_TAG}"
    slug = client.post("/mealie/recipes/create", json={
        "name": name, "ingredients": ["apples"], "instructions": ["Bake."],
    }).json()["slug"]
    try:
        from app.routers.printing import _mealie_get_recipe
        detail = await _mealie_get_recipe(slug)
        assert detail and detail["name"] == name
        assert detail["recipeInstructions"][0]["text"] == "Bake."
        assert await _mealie_get_recipe("no-such-recipe-" + _TAG) is None
    finally:
        client.delete(f"/recipes/{slug}")
