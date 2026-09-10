"""Recipe browse surfaces the source badge and made-before count end to end
(FoodAssistant-5frk, -bjps).

Drives the real app through TestClient: the /mealie/recipes list endpoint (with
Mealie mocked) carries a per-source badge and a batch cook count, and the Recipes
page JS renders those into a source chip and a "Made N times" note.
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

        with TestClient(app) as c:
            yield c
    finally:
        os.chdir(cwd)


@pytest.fixture(autouse=True)
def _fake_mealie(monkeypatch):
    """Two Mealie recipes: one native, one imported (carries orgURL)."""
    from app.services.mealie import MealieClient

    async def fake_search(self, search="", per_page=50):
        return [
            {"id": 1, "name": "Grandma Stew", "slug": "grandma-stew",
             "description": "", "totalTime": "", "rating": None},
            {"id": 2, "name": "Web Lasagna", "slug": "web-lasagna",
             "orgURL": "https://example.com/lasagna",
             "description": "", "totalTime": "", "rating": None},
        ]

    monkeypatch.setattr(MealieClient, "search_recipes", fake_search)


@pytest.fixture(autouse=True)
def _clean_counts():
    from app.database import SessionLocal
    from app.models.db_models import RecipeCookCount
    for _ in range(1):
        db = SessionLocal()
        db.query(RecipeCookCount).delete()
        db.commit()
        db.close()
    yield
    db = SessionLocal()
    db.query(RecipeCookCount).delete()
    db.commit()
    db.close()


def test_page_renders_source_and_made_helpers(client):
    page = client.get("/ui/recipes").text
    assert "function sourceBadge(" in page
    assert "function madeBadge(" in page
    assert "r.badge.css_class" in page
    assert "Made once" in page
    assert "Made ${n} times" in page
    # The old ad-hoc "community"/"web" literal badges are gone from the row.
    assert ">community<" not in page


def test_list_carries_badge_per_source(client):
    items = client.get("/mealie/recipes?mine=true&external=false").json()
    by_name = {r["name"]: r for r in items}
    assert by_name["Grandma Stew"]["badge"]["label"] == "My recipes"
    # A Mealie recipe with an original source URL reads as imported.
    assert by_name["Web Lasagna"]["badge"]["label"] == "Mealie (imported)"


def test_never_cooked_shows_zero_count(client):
    items = client.get("/mealie/recipes?mine=true&external=false").json()
    for r in items:
        assert r["cook_count"] == 0
        assert "last_cooked_at" not in r


def test_cooking_increments_count_in_list(client):
    from app.database import SessionLocal
    from app.services import cook_counts

    db = SessionLocal()
    cook_counts.record_cook(db, "mealie", slug="grandma-stew", title="Grandma Stew")
    cook_counts.record_cook(db, "mealie", slug="grandma-stew", title="Grandma Stew")
    db.close()

    items = client.get("/mealie/recipes?mine=true&external=false").json()
    by_name = {r["name"]: r for r in items}
    assert by_name["Grandma Stew"]["cook_count"] == 2
    assert by_name["Grandma Stew"]["last_cooked_at"]
    # A different recipe stays uncooked.
    assert by_name["Web Lasagna"]["cook_count"] == 0
