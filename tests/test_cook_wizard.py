"""Cook wizard option assembly and the /cook-wizard/options endpoint.

The wizard's guided path hands its picks straight to /mealie/suggest, so the
option values it offers must line up with what that route (and the external
search) already understand. These pure checks pin the shape and the value
alignment, plus a TestClient smoke of the options endpoint.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services.cook_wizard import wizard_options  # noqa: E402
from app.services import recipes_external  # noqa: E402


def test_options_shape():
    o = wizard_options()
    assert set(o.keys()) == {"cuisines", "categories", "diets"}
    assert set(o["cuisines"].keys()) == {"regions", "cuisines"}
    for group in (o["cuisines"]["regions"], o["cuisines"]["cuisines"],
                  o["categories"], o["diets"]):
        assert group, "each option group must be non-empty"
        for item in group:
            assert set(item.keys()) == {"value", "label", "emoji"}
            assert item["value"] and item["label"]


def test_options_return_fresh_copies():
    # Mutating the returned payload must not poison the module constants.
    a = wizard_options()
    a["categories"].clear()
    a["cuisines"]["regions"][0]["value"] = "tampered"
    b = wizard_options()
    assert b["categories"], "categories must survive a caller mutation"
    assert b["cuisines"]["regions"][0]["value"] != "tampered"


def test_specific_cuisine_values_match_external_areas():
    # Every specific-cuisine value must be one the external search filters on, so
    # the suggest call actually narrows the web results. (Broad regions like
    # African/Caribbean have no TheMealDB area; suggest handles those gracefully
    # by keeping untagged recipes, so they are allowed to fall through.)
    known = set(recipes_external._CUISINE_AREAS)
    for areas in recipes_external._CUISINE_AREAS.values():
        known |= areas
    o = wizard_options()
    for item in o["cuisines"]["cuisines"]:
        assert item["value"].lower() in known, (
            f"{item['value']} is not a known external cuisine/area"
        )


def test_emoji_are_bmp_or_older_food_glyphs():
    # Mirror the Cook page emoji rule (FoodAssistant-438t): stick to Emoji 12.0
    # (2019) or older so older devices render the glyphs. A cheap guard: reject
    # anything above the Unicode 13 food/flag block additions we know break.
    o = wizard_options()
    banned = {0x1FAD0, 0x1FAD1, 0x1FAD2, 0x1FAD3, 0x1FAD4, 0x1FAD5, 0x1FAD6,
              0x1F9C8, 0x1F9CA}  # 13.0+ food glyphs seen to fail
    for group in (o["cuisines"]["regions"], o["cuisines"]["cuisines"],
                  o["categories"], o["diets"]):
        for item in group:
            for ch in item["emoji"]:
                assert ord(ch) not in banned, f"{item['label']} uses a too-new glyph"


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True,
                        raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_options_endpoint(client):
    r = client.get("/cook-wizard/options")
    assert r.status_code == 200
    data = r.json()
    assert data["categories"]
    assert data["cuisines"]["regions"]
    # The endpoint mirrors the pure builder exactly.
    assert data == wizard_options()
