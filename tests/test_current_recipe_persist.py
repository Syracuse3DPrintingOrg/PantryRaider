"""Current recipe persistence + Cooked/Clear + leftovers
(FoodAssistant-yurm, -fu1u)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import current_recipe as cr  # noqa: E402


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    # Reset the module's in-process state so each test starts clean.
    cr._active = None
    cr._loaded = False
    yield tmp_path
    cr._active = None
    cr._loaded = False


def test_active_recipe_persists_across_restart(data_dir):
    cr.set_active({"title": "Chili", "source": "ai",
                   "ingredients": [{"name": "beans", "quantity": 2, "unit": "cup"}],
                   "steps": ["simmer"], "servings": 4})
    assert (data_dir / "current_recipe.json").exists()
    # Simulate a restart: drop in-memory state, force a fresh load from disk.
    cr._active = None
    cr._loaded = False
    got = cr.get_active()
    assert got is not None and got["title"] == "Chili"
    assert got["ingredients"][0]["name"] == "beans"


def test_scale_persists_and_survives_restart(data_dir):
    cr.set_active({"title": "Soup", "servings": 2,
                   "ingredients": [{"name": "stock", "quantity": 4}]})
    cr.scale_servings(2.0)
    cr._active = None
    cr._loaded = False
    got = cr.get_active()
    assert got["servings_scale"] == 2.0
    assert got["scaled_servings"] == 4  # 2 servings * 2.0
    assert got["ingredients"][0]["scaled_quantity"] == 8  # 4 * 2.0


def test_clear_removes_persisted_file(data_dir):
    cr.set_active({"title": "X", "ingredients": []})
    assert (data_dir / "current_recipe.json").exists()
    cr.clear_active()
    assert not (data_dir / "current_recipe.json").exists()
    assert cr.get_active() is None


# -- Cooked + leftovers via the API ----------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie.test", raising=False)
    monkeypatch.setattr(settings, "mealie_api_key", "k", raising=False)
    cr._active = None
    cr._loaded = False
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)
        cr._active = None
        cr._loaded = False


def test_cooked_raises_leftover_item_and_clears(client, monkeypatch):
    # Consume is best-effort; stub Grocy stock read to avoid a network call.
    from app.routers import current_recipe as cr_router

    async def _no_consume(recipe):
        return ["beans"]

    monkeypatch.setattr(cr_router, "_consume_active_recipe", _no_consume)
    client.post("/current-recipe", json={"title": "Tacos", "servings": 3,
                                         "ingredients": [{"name": "beans"}]})
    r = client.post("/current-recipe/cooked")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["consumed"] == ["beans"]
    assert body["action_item"]["kind"] == "leftover_prompt"
    assert "Tacos" in body["action_item"]["title"]
    # The recipe is cleared once cooked.
    assert client.get("/current-recipe").json()["recipe"] is None
    # The leftovers prompt is in the inbox.
    items = client.get("/action-items").json()["items"]
    assert any(i["kind"] == "leftover_prompt" for i in items)


def test_save_leftover_creates_grocy_item_and_resolves(client, monkeypatch):
    from app.services.grocy import GrocyClient
    captured = {}

    async def _import(self, item):
        captured["name"] = item.name
        captured["days"] = (item.best_by_date - __import__("datetime").date.today()).days
        return {"product_id": 42, "name": item.name}

    monkeypatch.setattr(GrocyClient, "import_item", _import)
    # Seed a leftover action item to resolve.
    from app.database import SessionLocal
    from app.services import action_items as ai
    db = SessionLocal()
    item = ai.create(db, ai.KIND_LEFTOVER_PROMPT, "Save Stew to leftovers?")
    db.close()
    r = client.post("/current-recipe/leftover",
                    json={"title": "Stew", "servings": 4, "days": 4,
                          "action_item_id": item["id"]})
    assert r.status_code == 200 and r.json()["product_id"] == 42
    assert captured["name"] == "Leftovers: Stew" and captured["days"] == 4
    # The originating action item is resolved (gone from the inbox).
    db = SessionLocal()
    assert ai.get(db, item["id"])["status"] == "done"
    db.close()
