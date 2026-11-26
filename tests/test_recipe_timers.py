"""Unit tests for the recipe duration parser (FoodAssistant-96h0).

The parser is pure: every case feeds a step string and asserts on the (label,
seconds) suggestions, plus the false-positive guards. No sleeping, no network.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "service"))

from app.services import current_recipe, recipe_timers  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    current_recipe.clear_active()
    yield
    current_recipe.clear_active()


def _seconds(step):
    return [s for _, s in recipe_timers.parse_step_durations(step)]


# --- supported phrase forms ----------------------------------------------


def test_simmer_minutes():
    out = recipe_timers.parse_step_durations("Simmer 20 minutes")
    assert out == [("Simmer", 1200)]


def test_bake_for_one_hour():
    out = recipe_timers.parse_step_durations("Bake for 1 hour")
    assert out == [("Bake", 3600)]


def test_rest_fractional_hours():
    out = recipe_timers.parse_step_durations("Rest 1.5 hours before slicing")
    assert out == [("Rest", 5400)]


def test_cook_seconds():
    assert _seconds("Cook 90 seconds") == [90]


def test_abbreviations_min_and_hr():
    assert _seconds("Saute 30 min") == [1800]
    assert _seconds("Roast 2 hr") == [7200]


def test_single_letter_units():
    assert _seconds("Boil 10 m") == [600]
    assert _seconds("Steam 45 s") == [45]


def test_an_hour_worded():
    out = recipe_timers.parse_step_durations("Let the dough rise for an hour")
    assert out == [("Let dough rise", 3600)]


def test_half_an_hour_worded():
    assert _seconds("Chill for half an hour") == [1800]


# --- range handling (upper bound) ----------------------------------------


def test_range_takes_upper_bound():
    # Documented choice: the upper bound, so the timer rings a touch late.
    assert _seconds("Bake 10-12 minutes") == [720]


def test_range_with_to_word():
    assert _seconds("Simmer 10 to 12 minutes") == [720]


# --- hour + minute combos ------------------------------------------------


def test_hr_min_combo_is_summed():
    out = recipe_timers.parse_step_durations("Braise 1 hr 30 min")
    assert out == [("Braise", 5400)]


def test_hour_minute_words_combo():
    assert _seconds("Cook 2 hours 15 minutes") == [8100]


def test_hour_and_minute_with_connector():
    assert _seconds("Roast 1 hour and 45 minutes") == [6300]


def test_two_separate_durations_not_merged():
    # Separated by other words, so they stay distinct suggestions.
    out = _seconds("Saute 5 minutes, then simmer 20 minutes")
    assert out == [300, 1200]


# --- false-positive guards -----------------------------------------------


def test_oven_temperature_yields_no_timer():
    assert recipe_timers.parse_step_durations("Preheat oven to 350 degrees") == []


def test_quantity_yields_no_timer():
    assert recipe_timers.parse_step_durations("Add 2 cups flour") == []


def test_plain_count_yields_no_timer():
    assert recipe_timers.parse_step_durations("Crack 3 eggs into the bowl") == []


def test_temperature_with_real_duration_only_keeps_duration():
    # The 350-degree temp never becomes a timer; only the 25 min does, and the
    # label is cut before the first number so "degrees" cannot leak into it.
    out = recipe_timers.parse_step_durations("Bake at 350 degrees for 25 minutes")
    assert out == [("Bake", 1500)]


def test_empty_step_is_empty():
    assert recipe_timers.parse_step_durations("") == []
    assert recipe_timers.parse_step_durations("   ") == []


# --- label derivation ----------------------------------------------------


def test_label_skips_leading_filler_and_truncates():
    label, _ = recipe_timers.parse_step_durations(
        "Simmer the tomato sauce gently for 20 minutes"
    )[0]
    # Filler dropped; the action and a word or two of its object, no adverb.
    assert label == "Simmer tomato sauce"


def test_label_falls_back_when_no_words():
    out = recipe_timers.parse_step_durations("20 minutes")
    assert out == [("Timer", 1200)]


@pytest.mark.parametrize("step,labels", [
    # The action the duration belongs to, not the first verb of the step.
    ("Heat the oven to 375 F and boil the noodles for 8 minutes.", ["Boil noodles"]),
    ("Cover with foil and bake for 45 minutes.", ["Bake"]),
    ("Brown the beef, then stir in the marinara and simmer for 10 minutes.", ["Simmer"]),
    ("Uncover, add the parmesan and bake for 10 minutes. Rest for 15 minutes.",
     ["Bake", "Rest"]),
    ("Saute 5 minutes, then simmer 20 minutes", ["Saute", "Simmer"]),
    ("Press the crumbs into a dish and bake the crust for 10 minutes.", ["Bake crust"]),
    ("Cool for 30 minutes, then chill for 3 hours before slicing.", ["Cool", "Chill"]),
    ("Cook, stirring, for 5 minutes", ["Cook"]),
    ("Let stand 10 minutes", ["Let stand"]),
    ("Whisk eggs with sugar until pale, about 3 minutes", ["Whisk eggs"]),
    ("Pan fry the cod for 4 minutes a side.", ["Fry cod"]),
    ("Place in the fridge for 4 hours", ["Place"]),
    ("Sauté the onions for 5 minutes", ["Saute onions"]),
])
def test_labels_name_the_action_the_time_belongs_to(step, labels):
    assert [l for l, _ in recipe_timers.parse_step_durations(step)] == labels


# --- picking one suggestion from what was said ----------------------------

PIE_STEPS = ["Heat the oven to 350 F.",
             "Press the crumbs into a dish and bake the crust for 10 minutes.",
             "Whisk the filling.",
             "Pour the filling in and bake for 15 minutes.",
             "Cool for 30 minutes, then chill for 3 hours."]


def _pie():
    return recipe_timers.suggestions_for_recipe({"steps": PIE_STEPS})


def test_choose_by_step_words_and_label():
    s = _pie()
    pick = recipe_timers.choose_suggestion
    assert pick(s, PIE_STEPS, step=2)["suggestion"]["label"] == "Bake crust"
    assert pick(s, PIE_STEPS, query="the crust")["suggestion"]["label"] == "Bake crust"
    assert pick(s, PIE_STEPS, query="chilling it")["suggestion"]["label"] == "Chill"
    assert pick(s, PIE_STEPS, step=5, label="Cool")["suggestion"]["seconds"] == 1800
    both = pick(s, PIE_STEPS, query="baking it")
    assert both["result"] == "ambiguous"
    assert both["choices"] == [{"label": "Bake crust", "seconds": 600, "step": 2},
                               {"label": "Bake", "seconds": 900, "step": 4}]
    assert pick(s, PIE_STEPS, step=5)["result"] == "ambiguous"
    assert pick(s, PIE_STEPS, step=3) == {"result": "not_found", "reason": "no_timer_in_step",
                                          "step": 3}
    assert pick(s, PIE_STEPS, query="frying")["reason"] == "no_match"
    assert pick([], [], query="bake")["reason"] == "no_timers"
    assert pick(s, PIE_STEPS)["result"] == "ambiguous"


def test_a_running_timer_is_found_by_name_and_length():
    s = {"label": "Bake", "seconds": 900, "step_index": 3}
    running = {"label": "bake", "total_seconds": 900.0, "expired": False, "state": "running"}
    assert recipe_timers.running_timer_for(s, [running]) is running
    assert recipe_timers.running_timer_for(s, [{**running, "total_seconds": 600}]) is None
    assert recipe_timers.running_timer_for(s, [{**running, "expired": True,
                                                "state": "ringing"}]) is None


# --- end-to-end over an active recipe ------------------------------------


def test_suggestions_for_recipe_walks_steps_in_order():
    current_recipe.set_active({
        "title": "Stew",
        "steps": [
            "Preheat oven to 350 degrees",      # no timer (temp)
            "Add 2 cups stock",                  # no timer (quantity)
            "Simmer 20 minutes",                 # one timer
            "Braise 1 hr 30 min",                # one merged timer
        ],
    })
    out = recipe_timers.suggestions_for_recipe(current_recipe.get_active())
    assert out == [
        {"label": "Simmer", "seconds": 1200, "step_index": 2},
        {"label": "Braise", "seconds": 5400, "step_index": 3},
    ]


def test_suggestions_for_recipe_empty_when_no_active():
    assert recipe_timers.suggestions_for_recipe(None) == []
    assert recipe_timers.suggestions_for_recipe(current_recipe.get_active()) == []


def test_suggestions_multiple_per_step_keep_index():
    current_recipe.set_active({
        "title": "x",
        "steps": ["Saute 5 minutes, then simmer 20 minutes"],
    })
    out = recipe_timers.suggestions_for_recipe(current_recipe.get_active())
    assert [s["seconds"] for s in out] == [300, 1200]
    assert all(s["step_index"] == 0 for s in out)


# --- start endpoint (uses the real timer service) ------------------------


def test_start_endpoint_creates_timer_from_suggestion(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app
    from app.services import timers

    # Make the app look configured so the setup-redirect middleware stays
    # out of the way. Historically this test only passed because an earlier
    # module leaked a configured grocy_base_url into the global settings;
    # with cross-test isolation in conftest.py it must configure itself.
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)

    timers.clear_all()
    current_recipe.set_active({"title": "x", "steps": ["Simmer 20 minutes"]})
    with TestClient(app) as client:
        # The suggestions endpoint offers the timer.
        sugg = client.get("/current-recipe/timer-suggestions").json()["suggestions"]
        assert sugg == [{"label": "Simmer", "seconds": 1200, "step_index": 0}]
        # Starting by step_index fills seconds from the suggestion.
        timer = client.post(
            "/current-recipe/timers/start", json={"step_index": 0}
        ).json()["timer"]
        assert timer["label"] == "Simmer"
        assert timer["total_seconds"] == 1200
        assert timer["running"] is True
        # And it really lives in the shared registry.
        assert any(t["id"] == timer["id"] for t in timers.list_timers())
    timers.clear_all()
