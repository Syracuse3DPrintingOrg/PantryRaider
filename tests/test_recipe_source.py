"""Recipe source badge mapping (FoodAssistant-5frk)."""
from __future__ import annotations

import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services.recipe_source import source_badge  # noqa: E402


def test_mealie_native_is_my_recipes():
    b = source_badge("mealie", has_source_url=False)
    assert b["label"] == "My recipes"
    assert "success" in b["css_class"]


def test_mealie_with_source_url_is_imported():
    b = source_badge("mealie", has_source_url=True)
    assert b["label"] == "Mealie (imported)"
    assert "primary" in b["css_class"]


def test_themealdb_and_spoonacular_are_web():
    assert source_badge("themealdb")["label"] == "Web"
    assert source_badge("spoonacular")["label"] == "Web"
    assert "secondary" in source_badge("themealdb")["css_class"]


def test_forager_is_community_cloud():
    b = source_badge("forager")
    assert b["label"] == "Forager cloud"
    assert "info" in b["css_class"]


def test_imported_vs_native_distinct():
    assert source_badge("mealie", True)["label"] != source_badge("mealie", False)["label"]


def test_unknown_or_missing_source_falls_back_to_web():
    # Total and pure: a new/blank source still gets a chip, never an error.
    assert source_badge("")["label"] == "Web"
    assert source_badge(None)["label"] == "Web"
    assert source_badge("some-future-source")["label"] == "Web"


def test_source_is_case_and_whitespace_insensitive():
    assert source_badge("  Forager ")["label"] == "Forager cloud"
    assert source_badge("MEALIE", True)["label"] == "Mealie (imported)"


def test_none_of_the_chips_use_the_pink_accent():
    # The brand accent maps to danger; source chips must stay quiet metadata.
    for src, url in [("mealie", False), ("mealie", True), ("themealdb", False),
                     ("forager", False), ("", False)]:
        assert "danger" not in source_badge(src, url)["css_class"]
