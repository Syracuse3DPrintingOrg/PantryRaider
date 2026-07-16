"""BLE shelf buttons (FoodAssistant-771d).

Covers the pure button decoders (BTHome v2 and unencrypted Xiaomi MiBeacon,
against synthetic frames built from the published formats), the press dedupe
logic, the ingest routing that keeps buttons out of the thermometer lists,
the registry endpoints, and the press-to-action execution (shopping add and
action tokens, with the cooldown) with Grocy and the action layer mocked.
No radio, bluez, or bleak needed.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SERVICE = REPO / "service"
sys.path.insert(0, str(SERVICE))

from foodassistant_gadgets import decoders  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import gadgets, gadgets_buttons, ha_events  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    gadgets.reset()
    gadgets_buttons.reset()
    ha_events.reset()
    yield
    gadgets.reset()
    gadgets_buttons.reset()
    ha_events.reset()


# -- BTHome v2 button decoder -------------------------------------------------

# A Shelly BLU Button1 style frame: device info (v2, plaintext), packet id 5,
# battery 100%, one button object with a single press.
BTHOME_SINGLE = bytes([0x40, 0x00, 0x05, 0x01, 0x64, 0x3A, 0x01])


def test_bthome_single_press_with_battery_and_counter():
    d = decoders.decode_bthome_button(BTHOME_SINGLE)
    assert d == {"battery": 100, "counter": 5, "buttons": 1,
                 "events": [{"button": 1, "event": "single"}]}


def test_bthome_double_long_and_hold_press_values():
    for raw, name in ((0x02, "double"), (0x03, "triple"), (0x04, "long"),
                      (0x05, "long_double"), (0x80, "hold")):
        d = decoders.decode_bthome_button(bytes([0x40, 0x3A, raw]))
        assert d["events"] == [{"button": 1, "event": name}]


def test_bthome_multi_button_keeps_button_index():
    # A BLU RC Button 4 style frame: four button objects, press on button 3.
    frame = bytes([0x40, 0x3A, 0x00, 0x3A, 0x00, 0x3A, 0x01, 0x3A, 0x00])
    d = decoders.decode_bthome_button(frame)
    assert d["buttons"] == 4
    assert d["events"][2] == {"button": 3, "event": "single"}
    assert [e["event"] for e in d["events"]] == ["none", "none", "single", "none"]


def test_bthome_skips_other_measurements_before_the_button():
    # Packet id, battery, a temperature object (0x02, sint16), then the press.
    frame = bytes([0x40, 0x00, 0x11, 0x01, 0x5A, 0x02, 0xC4, 0x09, 0x3A, 0x01])
    d = decoders.decode_bthome_button(frame)
    assert d["counter"] == 0x11 and d["battery"] == 0x5A
    assert d["events"] == [{"button": 1, "event": "single"}]


def test_bthome_encrypted_wrong_version_and_buttonless_rejected():
    # Encryption bit set: needs a bindkey, honestly unsupported.
    assert decoders.decode_bthome_button(bytes([0x41, 0x3A, 0x01])) is None
    # BTHome v1 (version bits 0).
    assert decoders.decode_bthome_button(bytes([0x00, 0x3A, 0x01])) is None
    # A BTHome sensor with no button object is not a button.
    assert decoders.decode_bthome_button(bytes([0x40, 0x02, 0xC4, 0x09])) is None
    assert decoders.decode_bthome_button(b"") is None


# -- Xiaomi MiBeacon button decoder --------------------------------------------

def _mibeacon(fc: int, obj: bytes) -> bytes:
    mac = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF]) if fc & 0x0010 else b""
    return fc.to_bytes(2, "little") + bytes([0x53, 0x01, 0x0D]) + mac + obj


# Frame control: version 4, MAC included, object included.
_FC_PLAIN = 0x4050
# Object: id 0x1001 (button), length 3, button 0, press type single.
_OBJ_SINGLE = bytes([0x01, 0x10, 0x03, 0x00, 0x00, 0x00])


def test_mibeacon_single_double_long_presses():
    d = decoders.decode_mibeacon_button(_mibeacon(_FC_PLAIN, _OBJ_SINGLE))
    assert d == {"battery": None, "counter": 0x0D, "buttons": 1,
                 "events": [{"button": 1, "event": "single"}]}
    for raw, name in ((0x01, "double"), (0x02, "long")):
        obj = bytes([0x01, 0x10, 0x03, 0x00, 0x00, raw])
        d = decoders.decode_mibeacon_button(_mibeacon(_FC_PLAIN, obj))
        assert d["events"] == [{"button": 1, "event": name}]


def test_mibeacon_second_button_is_one_based():
    obj = bytes([0x01, 0x10, 0x03, 0x01, 0x00, 0x00])
    d = decoders.decode_mibeacon_button(_mibeacon(_FC_PLAIN, obj))
    assert d["events"] == [{"button": 2, "event": "single"}]


def test_mibeacon_encrypted_objectless_and_non_button_rejected():
    # Encrypted bit set: needs a bindkey, honestly unsupported.
    assert decoders.decode_mibeacon_button(_mibeacon(_FC_PLAIN | 0x0008,
                                                     _OBJ_SINGLE)) is None
    # No object payload in the frame.
    assert decoders.decode_mibeacon_button(_mibeacon(0x4010, b"")) is None
    # A temperature object (0x1004) is not a button event.
    obj = bytes([0x04, 0x10, 0x02, 0xC4, 0x00])
    assert decoders.decode_mibeacon_button(_mibeacon(_FC_PLAIN, obj)) is None
    assert decoders.decode_mibeacon_button(b"\x50") is None


# -- Identification -------------------------------------------------------------

def test_identify_button_by_service_data():
    sd = {decoders.BTHOME_SERVICE_UUID: BTHOME_SINGLE}
    assert decoders.identify_button("SBBT-002C", service_data=sd) == "bthome_button"
    sd = {decoders.XIAOMI_SERVICE_UUID: _mibeacon(_FC_PLAIN, _OBJ_SINGLE)}
    assert decoders.identify_button(None, service_data=sd) == "xiaomi_button"
    # A BTHome temperature sensor is not a button.
    sd = {decoders.BTHOME_SERVICE_UUID: bytes([0x40, 0x02, 0xC4, 0x09])}
    assert decoders.identify_button("H&T", service_data=sd) is None
    assert decoders.identify_button("whatever") is None


def test_decode_button_dispatch():
    sd = {decoders.BTHOME_SERVICE_UUID: BTHOME_SINGLE}
    d = decoders.decode_button("bthome_button", sd)
    assert d and d["events"][0]["event"] == "single"
    sd = {decoders.XIAOMI_SERVICE_UUID: _mibeacon(_FC_PLAIN, _OBJ_SINGLE)}
    d = decoders.decode_button("xiaomi_button", sd)
    assert d and d["counter"] == 0x0D
    assert decoders.decode_button("bthome_button", {}) is None


# -- Dedupe ---------------------------------------------------------------------

def test_dedupe_repeats_with_a_counter_are_one_press():
    state: dict = {}
    decoded = {"counter": 5, "events": [{"button": 1, "event": "single"}]}
    assert decoders.dedupe_button_events(state, "AA", decoded, 100.0) == [
        {"button": 1, "event": "single"}]
    # The radio repeats the same packet: same counter, no new press.
    assert decoders.dedupe_button_events(state, "AA", decoded, 100.4) == []
    # A new press increments the counter and passes right away.
    decoded2 = {"counter": 6, "events": [{"button": 1, "event": "single"}]}
    assert decoders.dedupe_button_events(state, "AA", decoded2, 100.9) != []


def test_dedupe_without_a_counter_uses_a_window():
    state: dict = {}
    decoded = {"counter": None, "events": [{"button": 1, "event": "single"}]}
    assert decoders.dedupe_button_events(state, "AA", decoded, 100.0) != []
    assert decoders.dedupe_button_events(state, "AA", decoded, 101.5) == []
    assert decoders.dedupe_button_events(state, "AA", decoded, 103.0) != []


def test_dedupe_drops_none_events_and_is_per_device_and_event():
    state: dict = {}
    decoded = {"counter": None, "events": [{"button": 1, "event": "none"},
                                           {"button": 2, "event": "single"}]}
    assert decoders.dedupe_button_events(state, "AA", decoded, 100.0) == [
        {"button": 2, "event": "single"}]
    # A different device or press type is never suppressed by this one.
    other = {"counter": None, "events": [{"button": 2, "event": "single"}]}
    assert decoders.dedupe_button_events(state, "BB", other, 100.1) != []
    double = {"counter": None, "events": [{"button": 2, "event": "double"}]}
    assert decoders.dedupe_button_events(state, "AA", double, 100.2) != []


# -- Pure server helpers ----------------------------------------------------------

def test_normalize_mapping_shapes():
    m = gadgets_buttons.normalize_mapping(
        {"action": "shopping_add", "product_id": "7", "product_name": " Paper Towels "})
    assert m == {"action": "shopping_add", "product_name": "Paper Towels",
                 "product_id": 7}
    m = gadgets_buttons.normalize_mapping({"action": "esp_action", "token": "timer_eggs"})
    assert m == {"action": "esp_action", "token": "timer_eggs"}
    assert gadgets_buttons.normalize_mapping({"action": "shopping_add"}) is None
    assert gadgets_buttons.normalize_mapping({"action": "esp_action"}) is None
    assert gadgets_buttons.normalize_mapping({"action": "reboot"}) is None
    assert gadgets_buttons.normalize_mapping("junk") is None


def test_normalize_mappings_keeps_only_known_press_types():
    raw = {"single": {"action": "esp_action", "token": "timer_eggs"},
           "triple": {"action": "esp_action", "token": "x"},
           "double": {"action": "nope"}}
    out = gadgets_buttons.normalize_mappings(raw)
    assert list(out) == ["single"]


def test_cooldown_ok():
    fired = {"AA:single": 100.0}
    assert gadgets_buttons.cooldown_ok(fired, "AA:single", 103.0) is False
    assert gadgets_buttons.cooldown_ok(fired, "AA:single", 105.0) is True
    assert gadgets_buttons.cooldown_ok(fired, "AA:double", 100.1) is True


# -- Ingest routing and endpoints -------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd(); os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://g", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "gadgets_enabled", False, raising=False)
    monkeypatch.setattr(settings, "gadget_devices", [], raising=False)
    monkeypatch.setattr(settings, "buttons_enabled", False, raising=False)
    monkeypatch.setattr(settings, "button_devices", [], raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _press_payload(dev_id="F0:11:22:33:44:55", etype="single", counter=5):
    return {"devices": [{"id": dev_id, "kind": "button",
                         "protocol": "bthome_button", "name": "SBBT",
                         "battery": 88, "rssi": -61,
                         "event": {"button": 1, "type": etype,
                                   "counter": counter}}]}


def test_button_entries_stay_out_of_thermometer_lists(client):
    payload = _press_payload()
    payload["discovered"] = [{"id": "F0:AA:BB:CC:DD:EE", "kind": "button",
                              "protocol": "bthome_button", "name": "New fob",
                              "rssi": -70}]
    r = client.post("/gadgets/readings", json=payload).json()
    assert r["ok"] is True
    state = client.get("/gadgets/state").json()
    assert state["devices"] == [] and state["discovered"] == []
    found = state["button_discovered"]
    assert len(found) == 1 and found[0]["id"] == "F0:AA:BB:CC:DD:EE"
    assert found[0]["protocol"] == "bthome_button"


def test_add_button_enables_and_round_trips(client):
    r = client.post("/gadgets/buttons", json={
        "id": "f0:11:22:33:44:55", "name": "Pantry fob",
        "protocol": "bthome_button"}).json()
    assert r["ok"] is True
    data = client.get("/gadgets/config").json()
    assert data["buttons_enabled"] is True
    assert data["buttons"][0]["id"] == "F0:11:22:33:44:55"
    # Re-adding updates rather than duplicating.
    client.post("/gadgets/buttons", json={"id": "F0:11:22:33:44:55",
                                          "name": "Shelf"})
    buttons = client.get("/gadgets/config").json()["buttons"]
    assert len(buttons) == 1 and buttons[0]["name"] == "Shelf"
    # Once configured, the button leaves the discovered list.
    client.post("/gadgets/readings", json={
        "discovered": [{"id": "F0:11:22:33:44:55", "kind": "button",
                        "protocol": "bthome_button", "rssi": -60}]})
    assert client.get("/gadgets/state").json()["button_discovered"] == []


def test_mapping_set_clear_and_validation(client):
    client.post("/gadgets/buttons", json={"id": "F0:11:22:33:44:55",
                                          "protocol": "bthome_button"})
    r = client.post("/gadgets/buttons/mapping", json={
        "device_id": "f0:11:22:33:44:55", "event": "single",
        "action": "shopping_add", "product_id": 3,
        "product_name": "Paper Towels"}).json()
    assert r["ok"] is True
    state = client.get("/gadgets/state").json()
    mapping = state["buttons"][0]["mappings"]["single"]
    assert mapping["product_name"] == "Paper Towels" and mapping["product_id"] == 3
    assert "Paper Towels" in mapping["label"]
    # An action mapping on another press type.
    r = client.post("/gadgets/buttons/mapping", json={
        "device_id": "F0:11:22:33:44:55", "event": "long",
        "action": "esp_action", "token": "timer_eggs"}).json()
    assert r["ok"] is True
    # Clearing.
    r = client.post("/gadgets/buttons/mapping", json={
        "device_id": "F0:11:22:33:44:55", "event": "single", "action": ""}).json()
    assert r["ok"] is True
    assert "single" not in client.get("/gadgets/state").json()["buttons"][0]["mappings"]
    # Validation: a product-less shopping mapping and unknown press types fail.
    assert client.post("/gadgets/buttons/mapping", json={
        "device_id": "F0:11:22:33:44:55", "event": "single",
        "action": "shopping_add"}).json()["ok"] is False
    assert client.post("/gadgets/buttons/mapping", json={
        "device_id": "F0:11:22:33:44:55", "event": "triple",
        "action": "esp_action", "token": "x"}).json()["ok"] is False
    assert client.post("/gadgets/buttons/mapping", json={
        "device_id": "no:such", "event": "single", "action": ""}).json()["ok"] is False


def test_rename_and_remove_button(client):
    client.post("/gadgets/buttons", json={"id": "F0:11:22:33:44:55"})
    r = client.post("/gadgets/buttons/edit", json={
        "device_id": "F0:11:22:33:44:55", "name": "Paper towels shelf"}).json()
    assert r["ok"] is True and r["buttons"][0]["name"] == "Paper towels shelf"
    assert client.delete("/gadgets/buttons/f0:11:22:33:44:55").json()["ok"] is True
    assert client.get("/gadgets/config").json()["buttons"] == []


def test_state_snapshot_battery_and_last_press(client):
    client.post("/gadgets/buttons", json={"id": "F0:11:22:33:44:55",
                                          "name": "Fob",
                                          "protocol": "bthome_button"})
    monkey_enabled = client.post  # readability only
    client.post("/gadgets/readings", json=_press_payload())
    state = client.get("/gadgets/state").json()
    dev = state["buttons"][0]
    assert dev["battery"] == 88 and dev["battery_low"] is False
    assert dev["last_event"]["type"] == "single"
    assert dev["last_event"]["age_seconds"] is not None


# -- Event execution ----------------------------------------------------------------

def _configured(monkeypatch, tmp_path, mappings):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "buttons_enabled", True, raising=False)
    monkeypatch.setattr(settings, "button_devices", [
        {"id": "F0:11:22:33:44:55", "name": "Pantry fob",
         "protocol": "bthome_button", "mappings": mappings}], raising=False)


def test_press_adds_to_shopping_and_toasts(monkeypatch, tmp_path):
    _configured(monkeypatch, tmp_path, {
        "single": {"action": "shopping_add", "product_id": 3,
                   "product_name": "Paper Towels"}})
    calls = []

    async def fake_quick_add(item, quantity=1.0):
        calls.append(item)
        return "Shopping list"

    from app.services import shopping_source
    monkeypatch.setattr(shopping_source, "quick_add", fake_quick_add)
    result = asyncio.run(gadgets_buttons.handle_payload(_press_payload(), now=1000.0))
    assert result == {"events": 1, "executed": 1}
    assert calls == ["Paper Towels"]
    events = ha_events.poll()["events"]
    assert any("Paper Towels" in e.get("message", "") for e in events)


def test_cooldown_suppresses_a_rapid_second_press(monkeypatch, tmp_path):
    _configured(monkeypatch, tmp_path, {
        "single": {"action": "shopping_add", "product_name": "Paper Towels"}})
    calls = []

    async def fake_quick_add(item, quantity=1.0):
        calls.append(item)
        return "Shopping list"

    from app.services import shopping_source
    monkeypatch.setattr(shopping_source, "quick_add", fake_quick_add)
    asyncio.run(gadgets_buttons.handle_payload(
        _press_payload(counter=5), now=1000.0))
    # A new press (new counter) 2 seconds later: deduped app-side by cooldown.
    r = asyncio.run(gadgets_buttons.handle_payload(
        _press_payload(counter=6), now=1002.0))
    assert r == {"events": 1, "executed": 0}
    # Past the cooldown it executes again.
    r = asyncio.run(gadgets_buttons.handle_payload(
        _press_payload(counter=7), now=1006.0))
    assert r["executed"] == 1
    assert calls == ["Paper Towels", "Paper Towels"]


def test_press_fires_action_token(monkeypatch, tmp_path):
    _configured(monkeypatch, tmp_path, {
        "long": {"action": "esp_action", "token": "timer_eggs"}})
    tokens = []

    async def fake_fire_key(name, long=False):
        tokens.append(name)
        return {"ok": True, "detail": "Timer started."}

    from app.services import start_actions
    monkeypatch.setattr(start_actions, "fire_key", fake_fire_key)
    r = asyncio.run(gadgets_buttons.handle_payload(
        _press_payload(etype="long"), now=1000.0))
    assert r["executed"] == 1 and tokens == ["timer_eggs"]


def test_disabled_or_unmapped_press_executes_nothing(monkeypatch, tmp_path):
    _configured(monkeypatch, tmp_path, {})
    r = asyncio.run(gadgets_buttons.handle_payload(_press_payload(), now=1000.0))
    assert r == {"events": 1, "executed": 0}
    monkeypatch.setattr(settings, "buttons_enabled", False, raising=False)
    monkeypatch.setattr(settings, "button_devices", [
        {"id": "F0:11:22:33:44:55",
         "mappings": {"single": {"action": "esp_action", "token": "x"}}}],
        raising=False)
    r = asyncio.run(gadgets_buttons.handle_payload(
        _press_payload(counter=9), now=1010.0))
    assert r["executed"] == 0


def test_failed_shopping_add_toasts_a_warning(monkeypatch, tmp_path):
    _configured(monkeypatch, tmp_path, {
        "single": {"action": "shopping_add", "product_name": "Paper Towels"}})

    async def broken_quick_add(item, quantity=1.0):
        raise RuntimeError("Grocy is down")

    from app.services import shopping_source
    monkeypatch.setattr(shopping_source, "quick_add", broken_quick_add)
    r = asyncio.run(gadgets_buttons.handle_payload(_press_payload(), now=1000.0))
    assert r == {"events": 1, "executed": 0}
    events = ha_events.poll()["events"]
    assert any("did not work" in e.get("message", "") for e in events)


def test_test_fire_bypasses_cooldown(monkeypatch, tmp_path):
    _configured(monkeypatch, tmp_path, {
        "single": {"action": "shopping_add", "product_name": "Paper Towels"}})
    calls = []

    async def fake_quick_add(item, quantity=1.0):
        calls.append(item)
        return "Shopping list"

    from app.services import shopping_source
    monkeypatch.setattr(shopping_source, "quick_add", fake_quick_add)
    assert asyncio.run(gadgets_buttons.test_fire(
        "f0:11:22:33:44:55", "single"))["ok"] is True
    assert asyncio.run(gadgets_buttons.test_fire(
        "F0:11:22:33:44:55", "single"))["ok"] is True
    assert len(calls) == 2
    assert asyncio.run(gadgets_buttons.test_fire(
        "F0:11:22:33:44:55", "double"))["ok"] is False
    assert asyncio.run(gadgets_buttons.test_fire("no:such", "single"))["ok"] is False


# -- Pane render ------------------------------------------------------------------

def test_pane_renders_buttons_section(client, monkeypatch):
    from unittest.mock import patch
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/setup")
    assert r.status_code == 200
    html = r.text
    assert "Shelf buttons" in html
    assert 'id="button-devices"' in html
    assert 'id="buttons_enabled"' in html or 'buttons_enabled' in html
    assert "Listen for a press" in html
