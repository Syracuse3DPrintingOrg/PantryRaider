"""Equipment/utensil detection for recipes (FoodAssistant-ooq3)."""
from __future__ import annotations

import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import utensils  # noqa: E402


def _names(recipe):
    return [e["name"] for e in utensils.detect_equipment(recipe)]


def test_detects_appliances_and_utensils_from_steps():
    recipe = {
        "title": "Sheet-pan chicken",
        "ingredients": [{"name": "chicken"}, {"name": "olive oil"}],
        "steps": ["Preheat the oven to 425.", "Chop the onion.",
                  "Roast on a baking sheet until the thermometer reads 165."],
    }
    names = _names(recipe)
    assert "Oven" in names
    assert "Baking sheet" in names
    assert "Chef's knife" in names          # from "chop"
    assert "Meat thermometer" in names


def test_no_text_returns_empty():
    assert utensils.detect_equipment({"title": "", "ingredients": [], "steps": []}) == []


def test_missing_appliances_flags_unowned_only():
    recipe = {"title": "Sous vide steak",
              "steps": ["Sous vide the steak at 54C, then sear in a skillet."]}
    eq = utensils.detect_equipment(recipe)
    assert any(e["name"] == "Sous vide" for e in eq)
    # Owns a stove but not sous vide -> only Sous vide is flagged.
    missing = utensils.missing_appliances(eq, ["stove", "oven"])
    assert "Sous vide" in missing
    assert "Stovetop" not in missing        # owned
    assert "Skillet / frying pan" not in missing  # a plain utensil, never flagged


def test_missing_empty_when_kitchen_unset():
    recipe = {"steps": ["Air fry the wings."]}
    eq = utensils.detect_equipment(recipe)
    assert any(e["name"] == "Air fryer" for e in eq)
    # No owned list = fresh install: do not warn about everything.
    assert utensils.missing_appliances(eq, []) == []


def test_equipment_endpoint(tmp_path, monkeypatch):
    import os
    from unittest.mock import patch
    cwd = os.getcwd()
    os.chdir(SERVICE)
    try:
        from app.config import settings
        monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
        monkeypatch.setattr(settings, "auth_required", False, raising=False)
        monkeypatch.setattr(settings, "kitchen_appliances", ["stove"], raising=False)
        from app.services import current_recipe as cr
        cr._active = None
        cr._courses = {}
        cr._loaded = False
        from fastapi.testclient import TestClient
        from app.main import app
        with patch.object(type(settings), "is_configured", lambda self: True):
            c = TestClient(app)
            c.post("/current-recipe", json={"title": "Wings",
                                            "steps": ["Air fry the wings until crisp."]})
            data = c.get("/current-recipe/equipment").json()
        names = [e["name"] for e in data["equipment"]]
        assert "Air fryer" in names
        assert "Air fryer" in data["missing_appliances"]  # owns stove, not air fryer
    finally:
        os.chdir(cwd)
