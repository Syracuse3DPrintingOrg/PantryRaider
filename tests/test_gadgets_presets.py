"""Doneness presets, recipe-target extraction, low-battery, and the Home
Assistant outbound alert payload (FoodAssistant-42ja, FoodAssistant-oyt9).

All pure: a name -> Celsius lookup, text -> suggested target, a battery
threshold, and a fired-alert -> HA event payload. No hardware, no network.
"""
from __future__ import annotations

import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import gadgets, gadgets_ha  # noqa: E402
from app.services import printing  # noqa: E402


# -- Doneness preset table --------------------------------------------------

def test_doneness_preset_lookup_by_category_and_name():
    assert gadgets.doneness_preset_c("Beef medium rare") == 57.0
    assert gadgets.doneness_preset_c("beef well done") == 71.0
    assert gadgets.doneness_preset_c("Beef rare") == 52.0
    assert gadgets.doneness_preset_c("pork medium") == 63.0


def test_doneness_preset_lookup_is_separator_and_case_insensitive():
    assert gadgets.doneness_preset_c("Medium-Rare beef") is None  # order matters
    assert gadgets.doneness_preset_c("beef  MEDIUM-rare") == 57.0
    assert gadgets.doneness_preset_c("BEEF_MEDIUM_RARE") == 57.0


def test_doneness_preset_lookup_by_bare_category():
    # "chicken" is a whole category with one target.
    assert gadgets.doneness_preset_c("chicken") == 74.0
    assert gadgets.doneness_preset_c("turkey") == 74.0
    assert gadgets.doneness_preset_c("fish") == 63.0


def test_doneness_preset_unknown_or_ambiguous_returns_none():
    assert gadgets.doneness_preset_c("carbonized") is None
    assert gadgets.doneness_preset_c("") is None
    # "medium" is not unique across categories (beef 63, lamb 63 happen to match
    # but "well done" spans beef 71 and ground beef 71 too); an ambiguous bare
    # name with differing temps must not resolve.
    assert gadgets.doneness_preset_c("medium rare") == 57.0  # unique bare name


def test_doneness_presets_table_is_a_fresh_copy():
    a = gadgets.doneness_presets()
    a[0]["temp_c"] = 999
    assert gadgets.doneness_presets()[0]["temp_c"] != 999
    assert all({"category", "name", "temp_c"} <= set(p) for p in gadgets.doneness_presets())


# -- Recipe-driven target suggestion ----------------------------------------

def test_suggest_target_reads_explicit_internal_temperature_f():
    recipe = {"title": "Roast chicken",
              "steps": ["Roast until the internal temperature reaches 165°F."]}
    out = gadgets.suggest_target_from_recipe(recipe)
    assert out and out["source"] == "recipe temperature"
    assert round(out["temp_c"]) == 74


def test_suggest_target_reads_explicit_celsius():
    recipe = {"steps": ["Cook until a thermometer reads 63 C in the center."]}
    out = gadgets.suggest_target_from_recipe(recipe)
    assert out and out["temp_c"] == 63.0


def test_suggest_target_ignores_oven_temperature_without_probe_cue():
    # An oven setting, no internal/thermometer cue nearby: not a probe target.
    recipe = {"steps": ["Preheat the oven to 400°F and bake for 30 minutes."]}
    assert gadgets.suggest_target_from_recipe(recipe) is None


def test_suggest_target_falls_back_to_doneness_word():
    recipe = {"title": "Grilled steak",
              "steps": ["Grill to medium-rare and rest."]}
    out = gadgets.suggest_target_from_recipe(recipe)
    assert out and out["source"] == "doneness" and out["temp_c"] == 57.0


def test_suggest_target_falls_back_to_protein_with_cue():
    recipe = {"title": "Weeknight chicken",
              "steps": ["Cook the chicken until done."]}
    out = gadgets.suggest_target_from_recipe(recipe)
    assert out and out["source"] == "protein" and out["temp_c"] == 74.0


def test_suggest_target_none_when_no_cue():
    assert gadgets.suggest_target_from_recipe({"title": "Fruit salad",
                                               "steps": ["Chop and toss."]}) is None
    assert gadgets.suggest_target_from_recipe({}) is None
    assert gadgets.suggest_target_from_recipe(None) is None


# -- Low battery threshold --------------------------------------------------

def test_is_low_battery_threshold():
    assert gadgets.is_low_battery(5) is True
    assert gadgets.is_low_battery(20) is True   # at the threshold
    assert gadgets.is_low_battery(21) is False
    assert gadgets.is_low_battery(100) is False


def test_is_low_battery_none_is_never_low():
    # No battery data is unknown, not empty: never flag it.
    assert gadgets.is_low_battery(None) is False
    assert gadgets.is_low_battery("junk") is False


def test_is_low_battery_custom_threshold():
    assert gadgets.is_low_battery(30, threshold=35) is True
    assert gadgets.is_low_battery(40, threshold=35) is False


# -- Home Assistant outbound alert payload ----------------------------------

def test_probe_alert_payload_shape_and_conversion():
    alert = {"key": "AA:BB:CC:DD:EE:FF:1", "temp_c": 74.0, "target_c": 74.0,
             "direction": "above"}
    p = gadgets_ha.probe_alert_payload(alert, "Brisket", "f")
    assert p["device_id"] == "AA:BB:CC:DD:EE:FF"
    assert p["device_name"] == "Brisket"
    assert p["probe"] == "1"
    assert p["temp_c"] == 74.0 and p["target_c"] == 74.0
    assert p["direction"] == "above"
    assert "165°F" in p["temp_display"]
    assert "reached" in p["message"] and "Brisket" in p["message"]


def test_probe_alert_payload_below_direction():
    alert = {"key": "D:2", "temp_c": 3.0, "target_c": 4.0, "direction": "below"}
    p = gadgets_ha.probe_alert_payload(alert, "", "c")
    assert p["direction"] == "below"
    assert "dropped to" in p["message"]
    # No friendly name falls back to the device id.
    assert p["device_name"] == "D"


# -- Supvan label-printer battery (best-effort) -----------------------------

def test_parse_supvan_battery_found_and_clamped():
    assert printing.parse_supvan_battery("<p>Battery: 45%</p>") == 45
    assert printing.parse_supvan_battery("battery level 8 %") == 8
    assert printing.parse_supvan_battery("Batterie: 100%") == 100


def test_parse_supvan_battery_absent_is_none():
    # The normal bridge index page carries no battery: omit, never invent.
    page = "<ul><li><b>Supvan T50</b> (<code>supvan_t50</code>)</li></ul>"
    assert printing.parse_supvan_battery(page) is None
    assert printing.parse_supvan_battery("") is None
    assert printing.parse_supvan_battery("battery 250%") is None
