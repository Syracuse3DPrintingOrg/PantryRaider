"""Shopping quick-add typeahead (FoodAssistant-d0rb).

The manual "By name" add offers matching Mealie food names as you type. These
pins cover the pure prefix/substring matcher and the /mealie/foods/suggest
endpoint's fail-soft behavior when Mealie is not configured.
"""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services import mealie as mealie_svc
from app.services.mealie import MealieClient
from app.config import settings

_SERVICE_DIR = Path(__file__).parent.parent / "service"


@pytest.fixture(autouse=True)
def _fake_catalog(monkeypatch):
    """Populate the food-name cache without touching the network."""
    names = ["Olive oil", "Onion", "Oregano", "Garlic", "Green olives", "Flour"]
    monkeypatch.setattr(mealie_svc, "_food_names", sorted(names, key=str.lower))

    async def _noop(self):
        return None

    monkeypatch.setattr(MealieClient, "_ensure_catalog", _noop)
    yield


@pytest.mark.anyio
async def test_prefix_matches_come_first():
    out = await MealieClient().suggest_foods("o")
    # Every name that starts with "o" precedes the substring-only matches.
    assert out[:3] == ["Olive oil", "Onion", "Oregano"]
    assert "Green olives" in out  # contains "o", ranked after the prefix hits


@pytest.mark.anyio
async def test_matching_is_case_insensitive():
    out = await MealieClient().suggest_foods("GARL")
    assert out == ["Garlic"]


@pytest.mark.anyio
async def test_empty_prefix_returns_nothing():
    assert await MealieClient().suggest_foods("   ") == []


@pytest.mark.anyio
async def test_limit_caps_the_count():
    out = await MealieClient().suggest_foods("o", limit=2)
    assert out == ["Olive oil", "Onion"]


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
        s.auth_required = False
        with TestClient(app) as c:
            yield c
    finally:
        os.chdir(cwd)


def test_endpoint_empty_when_mealie_unconfigured(client, monkeypatch):
    monkeypatch.setattr(settings, "mealie_base_url", "")
    monkeypatch.setattr(settings, "mealie_api_key", "")
    r = client.get("/mealie/foods/suggest?q=oli")
    assert r.status_code == 200
    assert r.json() == {"suggestions": []}


def test_endpoint_empty_query(client, monkeypatch):
    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie.test")
    monkeypatch.setattr(settings, "mealie_api_key", "t")
    r = client.get("/mealie/foods/suggest?q=")
    assert r.status_code == 200
    assert r.json() == {"suggestions": []}


def test_endpoint_returns_matches(client, monkeypatch):
    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie.test")
    monkeypatch.setattr(settings, "mealie_api_key", "t")
    r = client.get("/mealie/foods/suggest?q=oli")
    assert r.status_code == 200
    # "Olive oil" starts with the query and leads; "Green olives" contains it.
    assert r.json()["suggestions"] == ["Olive oil", "Green olives"]
