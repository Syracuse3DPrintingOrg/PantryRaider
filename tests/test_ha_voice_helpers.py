"""The Home Assistant integration's voice helpers: spoken lengths, timer
names, per-device choices and when to ring. Pure, no Home Assistant needed."""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

COMPONENT = (Path(__file__).resolve().parents[1]
             / "homeassistant/custom_components/pantry_raider")
_spec = importlib.util.spec_from_file_location("pr_voice_helpers", COMPONENT / "voice_helpers.py")
vh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vh)


@pytest.mark.parametrize("said,seconds,name", [
    ("10 minutes", 600, ""),
    ("ten minute pasta", 600, "pasta"),
    ("pasta for 1 hour 30 minutes", 5400, "pasta"),
    ("an hour and a half", 5400, ""),
    ("two and a half minutes", 150, ""),
    ("half an hour", 1800, ""),
    ("twenty five minutes", 1500, ""),
    ("1h30m", 5400, ""),
    ("90 seconds", 90, ""),
    ("garlic bread", 0, "garlic bread"),
    ("", 0, ""),
])
def test_split_timer_request(said, seconds, name):
    assert vh.split_timer_request(said) == (seconds, name)


def test_parse_duration_takes_numbers_clocks_parts_and_words():
    assert vh.parse_duration(600) == 600
    assert vh.parse_duration("600") == 600
    assert vh.parse_duration("00:10:00") == 600
    assert vh.parse_duration("12:30") == 750
    assert vh.parse_duration({"hours": 1, "minutes": 5}) == 3900
    assert vh.parse_duration("10 minutes") == 600
    for bad in (0, -5, True, None, "soon", {"minutes": "x"}, 10 ** 9, "99 hours"):
        assert vh.parse_duration(bad) == 0


def test_lengths_and_labels_read_naturally():
    assert vh.spoken_duration(3900) == "1 hour and 5 minutes"
    assert vh.spoken_duration(545) == "9 minutes and 5 seconds"
    assert vh.spoken_duration(3630) == "1 hour and 1 minute"
    assert vh.spoken_duration(1) == "1 second"
    assert vh.timer_label("pasta", 600) == "Pasta"
    assert vh.timer_label("", 600) == "10 minute timer"
    assert vh.ring_message({"label": "Pasta"}) == "Your Pasta timer is done."
    assert vh.ring_message({"label": "10 minute timer"}) == "Your 10 minute timer is done."
    assert vh.ring_message({}) == "Your timer is done."


def test_a_device_nobody_set_up_keeps_its_timers_in_home_assistant():
    assert vh.routing_for({}, "assist_satellite.den") == {
        "timers": "home_assistant", "recipes": True, "screen": ""}
    assert vh.voice_options({})["timer_sentences"] is False
    assert vh.voice_options({})["recipe_sentences"] is True
    options = vh.with_satellite({"scan_interval": 30}, "assist_satellite.kitchen",
                                "pantry_raider", True, "ks-kitchen")
    assert options["scan_interval"] == 30
    assert vh.routing_for(options, "assist_satellite.kitchen") == {
        "timers": "pantry_raider", "recipes": True, "screen": "ks-kitchen"}
    # A second device is added beside the first, and a bad value falls back.
    options = vh.with_satellite(options, "assist_satellite.den", "nonsense", False, "")
    assert vh.routing_for(options, "assist_satellite.den")["timers"] == "home_assistant"
    assert vh.routing_for(options, "assist_satellite.kitchen")["screen"] == "ks-kitchen"


TIMERS = [
    {"id": 1, "label": "Pasta", "expired": False, "remaining_seconds": 300, "origin": "a"},
    {"id": 2, "label": "Garlic bread", "expired": False, "remaining_seconds": 60, "origin": "b"},
    {"id": 3, "label": "Eggs", "expired": True, "remaining_seconds": 0, "origin": "a"},
]


def test_pick_timers():
    assert [t["id"] for t in vh.pick_timers(TIMERS, "the pasta timer")] == [1]
    assert [t["id"] for t in vh.pick_timers(TIMERS, "bread")] == [2]
    assert vh.pick_timers(TIMERS, "soup") == []
    assert [t["id"] for t in vh.pick_timers(TIMERS, origin="b", ringing=False)] == [2]
    assert [t["id"] for t in vh.pick_timers(TIMERS, origin="nobody", ringing=False)] == [1, 2]
    assert [t["id"] for t in vh.pick_timers(TIMERS, ringing=True)] == [3]


def test_status_speech():
    assert vh.status_speech([]) == "No timers are running."
    assert vh.status_speech(TIMERS) == (
        "Garlic bread has 1 minute left. Pasta has 5 minutes left. Eggs is done.")


def test_ring_only_for_a_finished_timer_a_voice_device_set():
    event = {"type": "finished", "timer": {"id": 1, "origin": "assist_satellite.kitchen"}}
    assert vh.should_ring(event, {}) == "assist_satellite.kitchen"
    assert vh.should_ring({**event, "type": "started"}, {}) == ""
    assert vh.should_ring({"type": "finished", "timer": {"origin": "ks-kitchen"}}, {}) == ""
    assert vh.should_ring(event, {"voice": {"ring": False}}) == ""


def test_the_helpers_import_nothing_from_home_assistant():
    tree = ast.parse((COMPONENT / "voice_helpers.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("homeassistant")


def test_every_voice_service_is_described():
    import json
    import re
    src = (COMPONENT / "voice.py").read_text()
    names = set(re.findall(r'^SERVICE_[A-Z_]+ = "([a-z_]+)"', src, flags=re.M))
    assert len(names) == 7
    strings = json.loads((COMPONENT / "strings.json").read_text())
    english = json.loads((COMPONENT / "translations/en.json").read_text())
    yaml_text = (COMPONENT / "services.yaml").read_text()
    for name in names:
        assert name in strings["services"] and name in english["services"], name
        assert re.search(rf"^{name}:$", yaml_text, flags=re.M), name
    assert strings == english
