"""The configured Home Assistant calls and entities (services/ha_allowlist.py)."""
import json
from types import SimpleNamespace

import pytest

from app.services import gadgets_ha
from app.services import ha_allowlist as al
from app.services import ha_policy as hp


def _settings(**kw):
    base = dict(
        deployment_mode="server",
        streamdeck_key_overrides=[],
        streamdeck_ha_slots=[],
        streamdeck_cameras=[],
        gadget_ha_entities=[],
        ha_key_policy_version=0,
        upstream_ha_policy_version=0,
    )
    base.update(kw)
    ns = SimpleNamespace(**base)
    ns.is_satellite = lambda: ns.deployment_mode == "pi_remote"
    return ns


@pytest.fixture(autouse=True)
def _no_hygrometers(monkeypatch):
    monkeypatch.setattr(gadgets_ha, "configured_hygro_pairs", lambda: [])


SLOTS = [
    {"entity_id": "light.kitchen", "label": "Kitchen"},
    {"entity_id": "lock.front_door", "service": "lock.unlock", "label": "Door"},
    {"entity_id": "cover.garage", "service": "cover.open_cover", "label": "Garage",
     "allow_elevated": True},
]


# ---------- entry_calls ----------

def test_entry_calls_ha_action_and_media():
    assert al.entry_calls({"type": "ha_action", "entity_id": "light.a"}, []) == [
        ("light.a", "homeassistant.toggle")]
    assert al.entry_calls({"type": "ha_action", "service": "script.goodnight"}, []) == [
        ("script.goodnight", "script.goodnight")]
    assert al.entry_calls({"type": "media", "entity_id": "media_player.den",
                           "action": "next"}, []) == [
        ("media_player.den", "media_player.media_next_track")]


def test_entry_calls_macro_children():
    macro = {"type": "macro", "actions": "ha_1, timer_eggs, ha_2, ha_5, page_next"}
    assert al.entry_calls(macro, SLOTS) == [
        ("light.kitchen", "homeassistant.toggle"), ("lock.front_door", "lock.unlock")]
    assert al.entry_calls({"type": "macro", "actions": ["ha_3"]}, SLOTS) == [
        ("cover.garage", "cover.open_cover")]


def test_entry_calls_slot_dict_uses_its_position():
    assert al.entry_calls(SLOTS[1], SLOTS) == [("lock.front_door", "lock.unlock")]
    # An equal copy still finds its position.
    assert al.entry_calls(dict(SLOTS[0]), SLOTS) == [("light.kitchen", "homeassistant.toggle")]
    # A dict that is not one of the slots is not a slot.
    assert al.entry_calls({"entity_id": "lock.x"}, SLOTS) == []


def test_owned_calls_name_the_entry_a_press_checks():
    macro = {"type": "macro", "actions": "ha_1, ha_3"}
    assert al.owned_calls(macro, SLOTS) == [
        ("light.kitchen", "homeassistant.toggle", SLOTS[0]),
        ("cover.garage", "cover.open_cover", SLOTS[2])]
    key = {"type": "ha_action", "entity_id": "lock.x", "service": "lock.unlock"}
    assert al.owned_calls(key, SLOTS) == [("lock.x", "lock.unlock", key)]
    assert al.owned_calls(SLOTS[1], SLOTS) == [("lock.front_door", "lock.unlock", SLOTS[1])]
    # The macro is allowed through its slot even without its own flag, the
    # same answer describe_entry gives.
    for entity, service, owner in al.owned_calls(macro, SLOTS):
        assert hp.permitted(entity, service, owner, "deck", enforce=True)[0]
    assert al.describe_entry(macro, SLOTS)["allowed"] is True


def test_entry_calls_other_entries_are_empty():
    for entry in ({"type": "timer", "minutes": 5}, {"type": "page", "page": "x"},
                  {"type": "ha_action"}, "junk", None, {"type": "media"}):
        assert al.entry_calls(entry, SLOTS) == []


# ---------- configured_calls ----------

def test_configured_calls_from_overrides_slots_macros_and_profiles():
    lamp = {"id": "lamp", "type": "ha_action", "entity_id": "Light.A, light.b"}
    macro = {"id": "night", "type": "macro", "actions": ["ha_2"]}
    s = _settings(streamdeck_key_overrides=[lamp, macro], streamdeck_ha_slots=SLOTS)
    profile_key = {"id": "fan", "type": "ha_action", "entity_id": "fan.ceiling",
                   "service": "fan.toggle"}
    calls = al.configured_calls(s, [{"name": "Night", "key_overrides": [profile_key]}])
    assert calls[("light.a", "homeassistant.toggle")] is lamp
    assert calls[("light.b", "homeassistant.toggle")] is lamp
    assert calls[("fan.ceiling", "fan.toggle")] is profile_key
    # A macro child is governed by its slot's entry.
    assert calls[("lock.front_door", "lock.unlock")] is SLOTS[1]
    assert calls[("light.kitchen", "homeassistant.toggle")] is SLOTS[0]
    assert calls[("cover.garage", "cover.open_cover")] is SLOTS[2]
    assert len(calls) == 6


def test_configured_calls_prefers_allow_elevated():
    plain = {"id": "a", "type": "ha_action", "entity_id": "lock.front_door",
             "service": "lock.unlock"}
    allowed = dict(plain, id="b", allow_elevated=True)
    later = dict(plain, id="c")
    s = _settings(streamdeck_key_overrides=[plain, allowed, later])
    assert al.configured_calls(s, [])[("lock.front_door", "lock.unlock")] is allowed


def test_configured_calls_skips_invalid_calls():
    s = _settings(streamdeck_key_overrides=[
        {"id": "x", "type": "ha_action", "entity_id": "light.a,all"},
        {"id": "y", "type": "ha_action", "entity_id": "light.b", "service": "light/toggle"},
    ])
    assert al.configured_calls(s, []) == {}


def test_configured_calls_ignores_bad_profiles():
    s = _settings()
    assert al.configured_calls(s, [None, {"name": "x"}, {"key_overrides": "junk"}]) == {}


# ---------- readable entities, cameras, needs_control ----------

def test_readable_entities_includes_cameras_and_gadgets(monkeypatch):
    monkeypatch.setattr(gadgets_ha, "configured_hygro_pairs", lambda: [
        {"temperature": "sensor.fridge_temp", "humidity": "sensor.fridge_rh", "name": "Fridge"},
        {"temperature": "sensor.pantry_temp", "humidity": "", "name": "Pantry"},
    ])
    s = _settings(
        streamdeck_key_overrides=[{"id": "a", "type": "ha_action", "entity_id": "light.a"}],
        streamdeck_cameras=[{"name": "Door", "ha_entity": "Camera.Front_Door"},
                            {"name": "Manual", "snapshot_url": "http://192.168.1.40/snap.jpg"},
                            "junk"],
        gadget_ha_entities=["sensor.grill_probe_1", "not an id", ""],
    )
    assert al.camera_entities(s) == {"camera.front_door"}
    assert al.readable_entities(s, []) == {
        "light.a", "camera.front_door", "sensor.grill_probe_1",
        "sensor.fridge_temp", "sensor.fridge_rh", "sensor.pantry_temp"}


def test_needs_control():
    assert al.needs_control(_settings(), []) is False
    assert al.needs_control(_settings(streamdeck_ha_slots=[SLOTS[0]]), []) is True
    profile = {"name": "p", "key_overrides": [
        {"id": "a", "type": "media", "entity_id": "media_player.den"}]}
    assert al.needs_control(_settings(), [profile]) is True
    # Cameras and thermometers are read only.
    cams = _settings(streamdeck_cameras=[{"ha_entity": "camera.x"}],
                     gadget_ha_entities=["sensor.t"])
    assert al.needs_control(cams, []) is False


# ---------- load_profiles ----------

class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, rows):
        self._rows = rows

    def query(self, _model):
        return _FakeQuery(self._rows)


def test_load_profiles_skips_bad_rows():
    rows = [
        SimpleNamespace(name="Good", key_overrides=json.dumps([{"id": "a"}])),
        SimpleNamespace(name="Empty", key_overrides=None),
        SimpleNamespace(name="Broken", key_overrides="{not json"),
        SimpleNamespace(name="Dict", key_overrides=json.dumps({"id": "a"})),
    ]
    assert al.load_profiles(_FakeDb(rows)) == [
        {"name": "Good", "key_overrides": [{"id": "a"}]},
        {"name": "Empty", "key_overrides": []},
    ]


# ---------- policy_enforced ----------

def test_policy_enforced_on_a_server():
    assert al.policy_enforced(_settings()) is False
    assert al.policy_enforced(_settings(ha_key_policy_version=1)) is True
    # A satellite field means nothing on a server.
    assert al.policy_enforced(_settings(upstream_ha_policy_version=1)) is False
    assert al.policy_enforced(_settings(ha_key_policy_version=None)) is False
    assert al.policy_enforced(_settings(ha_key_policy_version="junk")) is False


def test_policy_enforced_on_a_satellite_follows_the_server():
    sat = dict(deployment_mode="pi_remote")
    assert al.policy_enforced(_settings(**sat)) is False
    # Its own migration flag does not turn it on.
    assert al.policy_enforced(_settings(ha_key_policy_version=1, **sat)) is False
    assert al.policy_enforced(_settings(upstream_ha_policy_version=1, **sat)) is True
    assert al.policy_enforced(_settings(upstream_ha_policy_version=2, **sat)) is True


def test_policy_enforced_without_is_satellite_method():
    s = SimpleNamespace(deployment_mode="pi_remote", upstream_ha_policy_version=1,
                        ha_key_policy_version=0)
    assert al.policy_enforced(s) is True


def test_policy_enforced_reads_the_real_settings(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "deployment_mode", "")
    monkeypatch.setattr(settings, "ha_key_policy_version", 0)
    assert al.policy_enforced(settings) is False
    monkeypatch.setattr(settings, "ha_key_policy_version", 1)
    assert al.policy_enforced(settings) is True


# ---------- describe_entry ----------

def test_describe_ok_entry():
    d = al.describe_entry({"type": "ha_action", "entity_id": "Light.A"}, [])
    assert d == {"verdict": "ok", "reason": "", "allowed": True, "needs_review": False,
                 "message": "", "calls": [{"entity_id": "light.a",
                                           "service": "homeassistant.toggle",
                                           "verdict": "ok", "reason": ""}]}


def test_describe_elevated_entry():
    entry = {"type": "ha_action", "entity_id": "lock.front_door", "service": "lock.unlock"}
    d = al.describe_entry(entry, [])
    assert (d["verdict"], d["reason"], d["allowed"], d["needs_review"]) == (
        "elevated", "high_risk", False, True)
    assert d["message"] == hp.MSG_HIGH_RISK
    d = al.describe_entry(dict(entry, allow_elevated=True), [])
    assert (d["verdict"], d["allowed"], d["needs_review"], d["message"]) == (
        "elevated", True, False, "")


def test_describe_invalid_and_no_call_entries():
    d = al.describe_entry({"type": "ha_action", "entity_id": "all"}, [])
    assert (d["verdict"], d["reason"], d["allowed"], d["message"]) == (
        "invalid", "entity", False, hp.MSG_INVALID)
    d = al.describe_entry({"type": "timer", "minutes": 3}, [])
    assert (d["verdict"], d["allowed"], d["calls"]) == ("ok", True, [])


def test_describe_invalid_wins_over_elevated():
    # A macro with an elevated child and an invalid child: the key cannot run
    # until it is fixed, so it does not ask for permission.
    slots = [{"entity_id": "lock.front_door", "service": "lock.unlock"},
             {"entity_id": "all", "service": "light.turn_off"}]
    d = al.describe_entry({"type": "macro", "actions": "ha_1,ha_2"}, slots)
    assert (d["verdict"], d["reason"], d["allowed"], d["needs_review"], d["message"]) == (
        "invalid", "entity", False, False, hp.MSG_INVALID)
    assert [c["verdict"] for c in d["calls"]] == ["elevated", "invalid"]


def test_describe_macro_uses_the_slot_entry():
    d = al.describe_entry({"type": "macro", "actions": "ha_1,ha_2"}, SLOTS)
    assert d["verdict"] == "elevated" and d["allowed"] is False
    d = al.describe_entry({"type": "macro", "actions": "ha_1,ha_3"}, SLOTS)
    assert d["verdict"] == "elevated" and d["allowed"] is True


# ---------- review_entries ----------

def test_review_entries_splits_errors_and_review_and_keeps_every_entry():
    entries = [
        {"id": "lamp", "slot": 0, "label": "Lamp", "type": "ha_action", "entity_id": "light.a",
         "needs_review": True},
        {"id": "door", "slot": 1, "label": "Door", "type": "ha_action",
         "entity_id": "lock.front_door", "service": "lock.unlock"},
        {"id": "garage", "slot": 2, "label": "Garage", "type": "ha_action",
         "entity_id": "cover.garage", "service": "cover.open_cover", "allow_elevated": True,
         "needs_review": True},
        {"id": "bad", "slot": 3, "label": "Bad", "type": "ha_action", "entity_id": "all"},
        {"id": "eggs", "slot": 4, "type": "timer", "minutes": 6, "needs_review": True},
        "junk",
    ]
    snapshot = json.loads(json.dumps(entries))
    out, errors, review = al.review_entries(entries, [])
    assert entries == snapshot, "the input is never changed"
    assert len(out) == len(entries)
    assert "needs_review" not in out[0]
    assert out[1]["needs_review"] is True
    assert "needs_review" not in out[2]
    assert out[4]["needs_review"] is True, "entries with no HA calls pass untouched"
    assert out[5] == "junk"
    assert errors == [{"id": "bad", "slot": 3, "label": "Bad", "message": hp.MSG_INVALID}]
    assert review == [{"id": "door", "slot": 1, "label": "Door",
                       "entity_id": "lock.front_door", "service": "lock.unlock",
                       "message": hp.MSG_HIGH_RISK}]


def test_review_entries_on_slots_uses_positions():
    slots = [dict(s) for s in SLOTS]
    out, errors, review = al.review_entries(slots, slots)
    assert errors == []
    assert [r["entity_id"] for r in review] == ["lock.front_door"]
    assert review[0]["slot"] == 1 and review[0]["label"] == "Door"
    assert out[1]["needs_review"] is True
    assert "needs_review" not in out[2]


def test_review_entries_handles_none():
    assert al.review_entries(None, None) == ([], [], [])


# ---------- token_calls ----------

def test_token_calls_for_a_slot():
    s = _settings(streamdeck_ha_slots=SLOTS)
    assert al.token_calls("ha_2", s) == [("lock.front_door", "lock.unlock", SLOTS[1])]
    assert al.token_calls("ha_5", s) == []


def test_token_calls_for_an_override_id():
    key = {"id": "night", "type": "ha_action", "entity_id": "light.a,light.b"}
    macro = {"id": "bedtime", "type": "macro", "actions": "ha_2,timer_rice"}
    s = _settings(streamdeck_key_overrides=[key, macro], streamdeck_ha_slots=SLOTS)
    assert al.token_calls("night", s) == [("light.a,light.b", "homeassistant.toggle", key)]
    assert al.token_calls("bedtime", s) == [("lock.front_door", "lock.unlock", SLOTS[1])]


def test_token_calls_for_timers_and_unknown_tokens():
    shadow = {"id": "timer_1", "type": "ha_action", "entity_id": "lock.x",
              "service": "lock.unlock"}
    s = _settings(streamdeck_key_overrides=[shadow], streamdeck_ha_slots=SLOTS)
    assert al.token_calls("timer_1", s) == []
    assert al.token_calls("timer_eggs", s) == []
    assert al.token_calls("nope", s) == []
    assert al.token_calls("", s) == []
