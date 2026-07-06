"""Server-side execution of deck-only Start Page keys (start_actions)."""
import asyncio

import pytest

from app.config import settings
from app.services import start_actions as sa
from app.services import timers


@pytest.fixture(autouse=True)
def _clean_timers():
    for t in timers.list_timers():
        timers.cancel_timer(t["id"])
    yield
    for t in timers.list_timers():
        timers.cancel_timer(t["id"])


# ---------- find_override ----------

def test_find_override_matches_id():
    ovs = [{"id": "a", "type": "timer"}, {"id": "b", "type": "media"}]
    assert sa.find_override("b", ovs)["type"] == "media"


def test_find_override_missing_and_bad_entries():
    assert sa.find_override("x", None) is None
    assert sa.find_override("x", ["junk", {"type": "media"}]) is None


# ---------- resolve_ha_call ----------

def test_ha_call_bare_entity_defaults_to_toggle():
    assert sa.resolve_ha_call({"entity_id": "light.kitchen"}) == (
        "light.kitchen", "homeassistant.toggle")


def test_ha_call_bare_service_implies_entity():
    assert sa.resolve_ha_call({"service": "script.goodnight"}) == (
        "script.goodnight", "script.goodnight")


def test_ha_call_entity_and_service():
    assert sa.resolve_ha_call({"entity_id": "light.kitchen", "service": "light.turn_on"}) == (
        "light.kitchen", "light.turn_on")


def test_ha_call_empty_is_none():
    assert sa.resolve_ha_call({}) is None
    assert sa.resolve_ha_call({"entity_id": " ", "service": ""}) is None


# ---------- resolve_media_call ----------

def test_media_call_maps_action_to_service():
    assert sa.resolve_media_call({"entity_id": "media_player.den", "action": "next"}) == (
        "media_player.den", "media_player.media_next_track")


def test_media_call_defaults_and_unknown_action_fall_back_to_play_pause():
    assert sa.resolve_media_call({"entity_id": "media_player.den"})[1] == \
        "media_player.media_play_pause"
    assert sa.resolve_media_call({"entity_id": "media_player.den", "action": "nope"})[1] == \
        "media_player.media_play_pause"


def test_media_call_without_entity_is_none():
    assert sa.resolve_media_call({"action": "next"}) is None


# ---------- resolve_ha_slot ----------

_SLOTS = [
    {"entity_id": "light.kitchen", "service": "light.toggle"},
    {"entity_id": "switch.fan"},
]


def test_ha_slot_resolves_in_order():
    assert sa.resolve_ha_slot("ha_1", _SLOTS) == ("light.kitchen", "light.toggle")
    assert sa.resolve_ha_slot("ha_2", _SLOTS) == ("switch.fan", "homeassistant.toggle")


def test_ha_slot_out_of_range_or_not_a_slot():
    assert sa.resolve_ha_slot("ha_3", _SLOTS) is None
    assert sa.resolve_ha_slot("ha_6", _SLOTS) is None
    assert sa.resolve_ha_slot("brightness", _SLOTS) is None
    assert sa.resolve_ha_slot("ha_1", []) is None
    assert sa.resolve_ha_slot("ha_1", [{"label": "no entity"}]) is None


# ---------- call_ha_service configuration guard ----------

def test_call_ha_unconfigured_reports_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "", raising=False)
    ok, detail = asyncio.run(sa.call_ha_service("light.kitchen", "light.toggle"))
    assert not ok
    assert "not configured" in detail


# ---------- fire_key dispatch (HA call stubbed) ----------

@pytest.fixture
def ha_recorder(monkeypatch):
    calls = []

    async def fake_call(entity_id, service):
        calls.append((entity_id, service))
        return True, f"Sent {service}"

    monkeypatch.setattr(sa, "call_ha_service", fake_call)
    return calls


def test_fire_unknown_key(monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_key_overrides", [], raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_slots", [], raising=False)
    res = asyncio.run(sa.fire_key("nope"))
    assert not res["ok"]
    assert "Unknown" in res["detail"]


def test_fire_ha_action_override(monkeypatch, ha_recorder):
    monkeypatch.setattr(settings, "streamdeck_ha_slots", [], raising=False)
    monkeypatch.setattr(settings, "streamdeck_key_overrides",
                        [{"id": "c1", "type": "ha_action", "entity_id": "light.kitchen"}],
                        raising=False)
    res = asyncio.run(sa.fire_key("c1"))
    assert res["ok"]
    assert ha_recorder == [("light.kitchen", "homeassistant.toggle")]


def test_fire_media_override(monkeypatch, ha_recorder):
    monkeypatch.setattr(settings, "streamdeck_ha_slots", [], raising=False)
    monkeypatch.setattr(settings, "streamdeck_key_overrides",
                        [{"id": "m1", "type": "media", "entity_id": "media_player.den",
                          "action": "volume_up"}],
                        raising=False)
    res = asyncio.run(sa.fire_key("m1"))
    assert res["ok"]
    assert ha_recorder == [("media_player.den", "media_player.volume_up")]


def test_fire_ha_slot_key(monkeypatch, ha_recorder):
    monkeypatch.setattr(settings, "streamdeck_ha_slots", _SLOTS, raising=False)
    monkeypatch.setattr(settings, "streamdeck_key_overrides", [], raising=False)
    res = asyncio.run(sa.fire_key("ha_2"))
    assert res["ok"]
    assert ha_recorder == [("switch.fan", "homeassistant.toggle")]


def test_fire_incomplete_overrides_report_config_problem(monkeypatch, ha_recorder):
    monkeypatch.setattr(settings, "streamdeck_ha_slots", [], raising=False)
    monkeypatch.setattr(settings, "streamdeck_key_overrides",
                        [{"id": "h", "type": "ha_action"},
                         {"id": "m", "type": "media"}],
                        raising=False)
    assert not asyncio.run(sa.fire_key("h"))["ok"]
    assert not asyncio.run(sa.fire_key("m"))["ok"]
    assert ha_recorder == []


def test_fire_page_typed_override_is_not_executed(monkeypatch, ha_recorder):
    # Camera-type keys still open a page; timer keys used to as well but now
    # fire against the shared registry (FoodAssistant-6ifu, tested below).
    monkeypatch.setattr(settings, "streamdeck_ha_slots", [], raising=False)
    monkeypatch.setattr(settings, "streamdeck_key_overrides",
                        [{"id": "c1", "type": "camera", "camera": "Front"}],
                        raising=False)
    res = asyncio.run(sa.fire_key("c1"))
    assert not res["ok"]
    assert "opens a page" in res["detail"]


# ---------- macros ----------

def test_macro_runs_slots_and_timer_presets_skips_deck_only(monkeypatch, ha_recorder):
    monkeypatch.setattr(settings, "streamdeck_ha_slots", _SLOTS, raising=False)
    monkeypatch.setattr(settings, "streamdeck_key_overrides",
                        [{"id": "mac", "type": "macro",
                          "actions": ["ha_1", "timer_eggs", "brightness"]}],
                        raising=False)
    res = asyncio.run(sa.fire_key("mac"))
    assert res["ok"]
    assert "Ran 2 of 3" in res["detail"]
    assert "brightness" in res["detail"]
    assert ha_recorder == [("light.kitchen", "light.toggle")]
    active = timers.list_timers()
    assert len(active) == 1
    assert active[0]["label"] == "Eggs"
    assert active[0]["total_seconds"] == 6 * 60


def test_macro_accepts_comma_separated_string(monkeypatch, ha_recorder):
    monkeypatch.setattr(settings, "streamdeck_ha_slots", _SLOTS, raising=False)
    monkeypatch.setattr(settings, "streamdeck_key_overrides",
                        [{"id": "mac", "type": "macro", "actions": "ha_1, ha_2"}],
                        raising=False)
    res = asyncio.run(sa.fire_key("mac"))
    assert res["ok"]
    assert len(ha_recorder) == 2


def test_macro_with_no_runnable_actions(monkeypatch, ha_recorder):
    monkeypatch.setattr(settings, "streamdeck_ha_slots", [], raising=False)
    monkeypatch.setattr(settings, "streamdeck_key_overrides",
                        [{"id": "mac", "type": "macro", "actions": ["brightness", "cook"]}],
                        raising=False)
    res = asyncio.run(sa.fire_key("mac"))
    assert not res["ok"]
    assert "Stream Deck" in res["detail"]
    assert ha_recorder == []


def test_macro_empty_actions(monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_key_overrides",
                        [{"id": "mac", "type": "macro", "actions": []}],
                        raising=False)
    res = asyncio.run(sa.fire_key("mac"))
    assert not res["ok"]
    assert "no actions" in res["detail"]


def test_macro_stops_on_first_ha_failure(monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_ha_slots", _SLOTS, raising=False)
    monkeypatch.setattr(settings, "streamdeck_key_overrides",
                        [{"id": "mac", "type": "macro",
                          "actions": ["ha_1", "timer_eggs"]}],
                        raising=False)

    async def fail_call(entity_id, service):
        return False, "Could not reach Home Assistant."

    monkeypatch.setattr(sa, "call_ha_service", fail_call)
    res = asyncio.run(sa.fire_key("mac"))
    assert not res["ok"]
    assert "Stopped at ha_1" in res["detail"]
    assert timers.list_timers() == []


# ---------- timer keys fire against the shared registry (6ifu) ----------

def test_preset_timer_key_starts_the_shared_timer(monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_ha_slots", [], raising=False)
    monkeypatch.setattr(settings, "streamdeck_key_overrides", [], raising=False)
    res = asyncio.run(sa.fire_key("timer_eggs"))
    assert res["ok"] and "Eggs started: 6:00" in res["detail"]
    active = timers.list_timers()
    assert len(active) == 1 and active[0]["label"] == "Eggs"
    assert active[0]["total_seconds"] == 6 * 60


def test_timer_key_short_press_adds_a_minute(monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_ha_slots", [], raising=False)
    monkeypatch.setattr(settings, "streamdeck_key_overrides", [], raising=False)
    assert "Timer 1 started: 1:00" in asyncio.run(sa.fire_key("timer_1"))["detail"]
    assert "+1:00" in asyncio.run(sa.fire_key("timer_1"))["detail"]
    assert "+1:00" in asyncio.run(sa.fire_key("timer_1"))["detail"]
    active = timers.list_timers()
    assert len(active) == 1 and active[0]["total_seconds"] == 3 * 60


def test_timer_key_long_press_resets(monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_ha_slots", [], raising=False)
    monkeypatch.setattr(settings, "streamdeck_key_overrides", [], raising=False)
    asyncio.run(sa.fire_key("timer_eggs"))
    assert len(timers.list_timers()) == 1
    res = asyncio.run(sa.fire_key("timer_eggs", long=True))
    assert res["ok"] and "reset" in res["detail"]
    assert timers.list_timers() == []
    # Long press on an idle key reports, never creates.
    res = asyncio.run(sa.fire_key("timer_1", long=True))
    assert res["ok"] and "not running" in res["detail"]
    assert timers.list_timers() == []


def test_preset_key_running_press_extends(monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_ha_slots", [], raising=False)
    monkeypatch.setattr(settings, "streamdeck_key_overrides", [], raising=False)
    asyncio.run(sa.fire_key("timer_pasta"))          # 10:00
    res = asyncio.run(sa.fire_key("timer_pasta"))    # +1:00
    assert "+1:00" in res["detail"]
    assert timers.list_timers()[0]["total_seconds"] == 11 * 60


def test_expired_timer_key_dismisses(monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_ha_slots", [], raising=False)
    monkeypatch.setattr(settings, "streamdeck_key_overrides", [], raising=False)
    t = timers.create_timer("Eggs", 60)
    row = timers._timers[t["id"]]
    row.deadline_epoch -= 120
    res = asyncio.run(sa.fire_key("timer_eggs"))
    assert res["ok"] and "dismissed" in res["detail"]
    assert timers.list_timers() == []


def test_custom_timer_override_fires(monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_ha_slots", [], raising=False)
    monkeypatch.setattr(settings, "streamdeck_key_overrides",
                        [{"id": "t9", "type": "timer", "label": "Tea", "minutes": 3}],
                        raising=False)
    res = asyncio.run(sa.fire_key("t9"))
    assert res["ok"] and "Tea started: 3:00" in res["detail"]
    assert timers.list_timers()[0]["label"] == "Tea"
