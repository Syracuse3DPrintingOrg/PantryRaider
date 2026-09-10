"""Recipe tuning and storage-category placement on the Settings page.

After the settings reorganization (docs/design/settings-reorg.md, iteration
2) the recipe suggestion tuning lives in the Personalization menu's Recipe
Preferences pane and the custom storage categories in the Settings menu's
Inventory & Storage pane, each with its own save wiring. This suite guards
that structure and the stand-mixer attachment toggle (FoodAssistant-rjdr)
on the appliances checklist.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

from app.config import settings  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app

    cwd = os.getcwd()
    os.chdir(_SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_password", "")
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _render(client, monkeypatch, *, satellite: bool) -> str:
    monkeypatch.setattr(
        settings, "deployment_mode", "pi_remote" if satellite else "server"
    )
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/setup")
    assert r.status_code == 200
    return r.text


def test_intent_group_pills_present(client, monkeypatch):
    html = _render(client, monkeypatch, satellite=False)
    # The taste-level settings live in the two-menu intent groups now.
    # After FoodAssistant-ysj1 the recipe taste tuning split into its own
    # pane-recipe-tuning, apart from the pane-personalization-recipes
    # connection settings, so both panes render.
    for pane in ("pane-appearance", "pane-personalization-recipes",
                 "pane-recipe-tuning", "pane-inventory"):
        assert f'data-bs-target="#{pane}"' in html
        assert f'id="{pane}"' in html
    # The dissolved panes are gone (their hashes alias).
    for pane in ("pane-recipes", "pane-personalization-storage"):
        assert f'id="{pane}"' not in html


def test_recipe_connection_and_tuning_live_in_separate_panes(client, monkeypatch):
    """FoodAssistant-ysj1 split the recipe settings so taste tuning cannot
    overwrite the Mealie connection. The Recipes pane (pane-personalization-
    recipes) holds only the connection settings (Mealie + external sources)
    with savePaneRecipes; the taste tuning + appliances live in their own
    Recipe suggestions pane (pane-recipe-tuning) with savePaneRecipePrefs.
    Each pane owns exactly one Save button, so a save in one pane can never
    include the other pane's fields. The Cameras pane (pane-connections)
    still holds no Mealie fields."""
    html = _render(client, monkeypatch, satellite=False)
    conn_pane = html.split('id="pane-personalization-recipes"', 1)[1] \
                    .split('id="pane-', 1)[0]
    tune_pane = html.split('id="pane-recipe-tuning"', 1)[1] \
                    .split('id="pane-', 1)[0]
    # Connection settings: only in the Recipes pane, with only its Save button.
    for field in ("mealie_base_url", "mealie_api_key", "recipe_source",
                  "themealdb_api_key", "spoonacular_api_key"):
        assert field in conn_pane, f"{field} missing from Recipes pane"
        assert field not in tune_pane, f"{field} leaked into tuning pane"
    assert 'onclick="savePaneRecipes(this)"' in conn_pane
    assert 'onclick="savePaneRecipePrefs(this)"' not in conn_pane
    # Taste tuning: only in the Recipe suggestions pane, with only its button.
    for field in ("staple_items", "cook_ai_context", "kitchen-appliances",
                  "perishable_days", "expiring_soon_days", "suggest_per_tier"):
        assert field in tune_pane, f"{field} missing from tuning pane"
        assert field not in conn_pane, f"{field} leaked into Recipes pane"
    assert 'onclick="savePaneRecipePrefs(this)"' in tune_pane
    assert 'onclick="savePaneRecipes(this)"' not in tune_pane
    conn = html.split('id="pane-connections"', 1)[1].split('id="pane-', 1)[0]
    assert "mealie_base_url" not in conn


def test_save_scopes_are_disjoint(client, monkeypatch):
    """The two save handlers post disjoint field sets, so saving taste tuning
    never re-posts the Mealie connection fields and vice versa
    (FoodAssistant-ysj1). Guards the JS payload builders directly."""
    js = client.get("static/js/setup/panes.js").text
    # Isolate each save function body.
    recipes = js.split("function savePaneRecipes(", 1)[1].split("\n}", 1)[0]
    prefs = js.split("function savePaneRecipePrefs(", 1)[1].split("\n}", 1)[0]
    connection_fields = ("mealie_base_url", "mealie_api_key", "recipe_source")
    tuning_fields = ("staple_items", "cook_ai_context", "kitchen_appliances",
                     "perishable_days", "suggest_per_tier")
    for f in connection_fields:
        assert f in recipes, f"{f} should be in savePaneRecipes"
        assert f not in prefs, f"{f} must not be in savePaneRecipePrefs"
    for f in tuning_fields:
        assert f in prefs, f"{f} should be in savePaneRecipePrefs"
        assert f not in recipes, f"{f} must not be in savePaneRecipes"


def test_storage_categories_live_in_inventory_pane(client, monkeypatch):
    html = _render(client, monkeypatch, satellite=False)
    inv = html.split('id="pane-inventory"', 1)[1].split('id="pane-', 1)[0]
    assert "storage-cat-editor" in inv
    assert "saveStorageCategories()" in inv


def test_weather_has_no_dedicated_settings_pane(client, monkeypatch):
    # Weather no longer has its own Personalization section; location/units are
    # set on the Weather page itself (Pantry Raider). A hidden input keeps the
    # Stream Deck save working.
    html = _render(client, monkeypatch, satellite=False)
    assert 'id="pane-personalization-weather"' not in html
    assert 'data-bs-target="#pane-personalization-weather"' not in html
    assert 'id="streamdeck_weather_location"' in html  # hidden mirror for the deck save


def test_weather_page_has_location_settings(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "grocy_base_url", "http://g")
    monkeypatch.setattr(settings, "grocy_api_key", "k")
    with patch.object(type(settings), "is_configured", lambda self: True):
        html = client.get("/ui/weather").text
    assert 'id="wxLocation"' in html
    assert 'id="wxUnits"' in html
    assert 'onclick="saveWeatherSettings(this)"' in html


def test_satellite_recipe_prefs_are_read_only(client, monkeypatch):
    """On a satellite the recipe tuning still renders, read-only, with the
    managed-on-server note and no editable save button (server-managed).
    After FoodAssistant-ysj1 the tuning lives in pane-recipe-tuning."""
    html = _render(client, monkeypatch, satellite=True)
    assert 'id="pane-recipe-tuning"' in html
    # The storage-categories editor is gated out on a satellite.
    assert 'id="storage-cat-editor"' not in html
    pane = html.split('id="pane-recipe-tuning"', 1)[1] \
               .split('id="pane-', 1)[0]
    assert 'onclick="savePaneRecipePrefs(this)"' not in pane
    assert "Recipe settings are managed on the main server" in pane


def test_stand_mixer_attachment_toggle_present(client, monkeypatch):
    """The attachments group is wired to show only when a stand mixer is owned."""
    html = _render(client, monkeypatch, satellite=False)
    assert 'data-group="attachment"' in html
    assert "function syncStandMixerAttachments" in client.get(
        "static/js/setup/panes.js").text
    # Wired to the stand_mixer checkbox on load and change (menu.js page init).
    assert "appliance_stand_mixer" in html
    assert "syncStandMixerAttachments" in client.get(
        "static/js/setup/menu.js").text


def test_settings_search_box_present(client, monkeypatch):
    """The single grouped menu carries a search box that filters the pills and
    highlights matching cards; the four plain-language group headings frame it
    (FoodAssistant-kjk8)."""
    from app.config import settings
    monkeypatch.setattr(settings, "deployment_mode", "server")
    with patch.object(type(settings), "is_configured", lambda self: True):
        html = client.get("/setup").text
    assert 'id="settings-search"' in html
    menu_js = client.get("static/js/setup/menu.js").text
    assert "function settingsSearch(" in menu_js
    for heading in ("Kitchen", "This Device", "Connections", "System"):
        assert f'class="menu-heading">{heading}<' in html
    # The old two-menu Personalization/Settings toggle is gone.
    assert 'data-mgroup=' not in html
    assert "function showSettingsMenu(" not in menu_js
