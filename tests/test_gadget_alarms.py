"""Left-open fridge and freezer alarms (FoodAssistant-5c61).

Turns the stored hygrometer thresholds into protection: a reading outside
its range for longer than the grace period alarms, a door contact sensor
open past its limit alarms, a sensor gone silent past its staleness window
alarms, and everything clears on recovery. Covers the pure contact decoders
(BTHome v2, SwitchBot Contact, unencrypted Xiaomi MiBeacon), the alarm
evaluation matrix, the warning-channel edge trigger, the registry CRUD, and
the Cub summary escalation. No radio, bluez, or bleak needed.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SERVICE = REPO / "service"
sys.path.insert(0, str(SERVICE))

from foodassistant_gadgets import decoders  # noqa: E402
from foodassistant_gadgets import config as gd_config  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import gadgets, ha_events  # noqa: E402
from app.services import cub as cub_svc  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    gadgets.reset()
    ha_events.reset()
    yield
    gadgets.reset()
    ha_events.reset()


# -- BTHome v2 decoder (Shelly BLU Door/Window and kin) -------------------------

# A Shelly BLU Door/Window-shaped frame: device info (v2, trigger-based),
# battery 100 %, illuminance, window open, rotation.
BTHOME_OPEN = bytes([0x44, 0x01, 0x64, 0x05, 0x10, 0x27, 0x00,
                     0x2D, 0x01, 0x3F, 0x00, 0x00])
BTHOME_CLOSED = bytes([0x44, 0x01, 0x64, 0x2D, 0x00])


def test_bthome_v2_window_open_and_closed():
    r = decoders.decode_bthome_v2(BTHOME_OPEN)
    assert r == {"open": True, "battery_pct": 100}
    r = decoders.decode_bthome_v2(BTHOME_CLOSED)
    assert r == {"open": False, "battery_pct": 100}


def test_bthome_v2_other_opening_object_ids():
    for obj in (0x11, 0x1A, 0x1B):   # opening, door, garage door
        assert decoders.decode_bthome_v2(bytes([0x40, obj, 0x01]))["open"] is True


def test_bthome_rejects_encrypted_and_wrong_version():
    # Bit 0 set = encrypted (needs a key we do not have).
    assert decoders.decode_bthome_v2(bytes([0x45, 0x2D, 0x01])) is None
    # Version 1 in the top bits.
    assert decoders.decode_bthome_v2(bytes([0x20, 0x2D, 0x01])) is None
    assert decoders.decode_bthome_v2(b"") is None


def test_bthome_unknown_object_stops_the_walk_quietly():
    # window open, then an id we have no length for: keep what was decoded.
    r = decoders.decode_bthome_v2(bytes([0x40, 0x2D, 0x01, 0xF8, 0x99]))
    assert r["open"] is True


def test_bthome_button_only_frame_is_not_a_contact():
    # A Shelly BLU Button frame (button event object, no opening object)
    # decodes with open None, so identify_contact never claims it and the
    # concurrent button class keeps it.
    frame = bytes([0x40, 0x3A, 0x01])
    assert decoders.decode_bthome_v2(frame)["open"] is None
    assert decoders.identify_contact(
        None, None, {decoders.BTHOME_SERVICE_UUID: frame}) is None


# -- SwitchBot Contact Sensor decoder -------------------------------------------

def test_switchbot_contact_open_closed_and_timeout():
    # Device type 'd' (0x64), battery 89, open bit set.
    r = decoders.decode_switchbot_contact(bytes([0x64, 0x00, 89, 0x02, 0, 0, 0, 0, 0]))
    assert r == {"open": True, "battery_pct": 89, "timeout": False}
    r = decoders.decode_switchbot_contact(bytes([0x64, 0x40, 89, 0x00, 0, 0, 0, 0, 0]))
    assert r["open"] is False
    # The sensor's own held-open-timeout bit also reads as open.
    r = decoders.decode_switchbot_contact(bytes([0x64, 0x00, 89, 0x04, 0, 0, 0, 0, 0]))
    assert r["open"] is True and r["timeout"] is True


def test_switchbot_contact_rejects_meters_and_short_frames():
    # 0x54 'T' is a Meter, which stays a hygrometer.
    assert decoders.decode_switchbot_contact(bytes([0x54, 0, 90, 0x02])) is None
    assert decoders.decode_switchbot_contact(bytes([0x64, 0, 90])) is None


# -- Xiaomi MiBeacon decoder (unencrypted only) ----------------------------------

def _mibeacon(obj: bytes, frame_ctl: int = 0x0050) -> bytes:
    # frame control (LE), product id, counter, MAC (flagged), then the object.
    return (frame_ctl.to_bytes(2, "little") + bytes([0x8D, 0x0A, 0x01])
            + b"\xAA" * 6 + obj)


def test_xiaomi_door_states():
    assert decoders.decode_xiaomi_contact(
        _mibeacon(bytes([0x19, 0x10, 0x01, 0x00])))["open"] is True
    assert decoders.decode_xiaomi_contact(
        _mibeacon(bytes([0x19, 0x10, 0x01, 0x01])))["open"] is False
    # 2 = "not closed after timeout": still open.
    assert decoders.decode_xiaomi_contact(
        _mibeacon(bytes([0x19, 0x10, 0x01, 0x02])))["open"] is True
    # 3 = device reset: no door state.
    assert decoders.decode_xiaomi_contact(
        _mibeacon(bytes([0x19, 0x10, 0x01, 0x03]))) is None


def test_xiaomi_battery_object_and_encrypted_rejection():
    r = decoders.decode_xiaomi_contact(_mibeacon(bytes([0x0A, 0x10, 0x01, 0x63])))
    assert r == {"open": None, "battery_pct": 99}
    # The encryption bit (0x0008) means a bindkey device: not supported.
    assert decoders.decode_xiaomi_contact(
        _mibeacon(bytes([0x19, 0x10, 0x01, 0x00]), frame_ctl=0x0058)) is None
    assert decoders.decode_xiaomi_contact(b"\x00") is None


# -- Identification ---------------------------------------------------------------

def test_identify_contact_by_service_data():
    assert decoders.identify_contact(
        None, None, {decoders.BTHOME_SERVICE_UUID: BTHOME_OPEN}) == "bthome_contact"
    assert decoders.identify_contact(
        None, None,
        {"0000fd3d-0000-1000-8000-00805f9b34fb":
         bytes([0x64, 0x00, 89, 0x02, 0, 0, 0, 0, 0])}) == "switchbot_contact"
    assert decoders.identify_contact(
        None, None,
        {decoders.XIAOMI_SERVICE_UUID:
         _mibeacon(bytes([0x19, 0x10, 0x01, 0x00]))}) == "xiaomi_contact"
    # A SwitchBot Meter keeps its hygrometer identity.
    meter = bytes([0x54, 0x00, 90, 0x02, 0x80 | 22, 45])
    sd = {"0000fd3d-0000-1000-8000-00805f9b34fb": meter}
    assert decoders.identify_contact(None, None, sd) is None
    assert decoders.identify_hygrometer(None, None, sd) == "switchbot_meter"
    assert decoders.identify_contact("whatever") is None


def test_decode_contact_dispatch():
    r = decoders.decode_contact(
        "bthome_contact", None, {decoders.BTHOME_SERVICE_UUID: BTHOME_CLOSED})
    assert r["open"] is False
    assert decoders.decode_contact("bogus", {}, {}) is None


# -- Daemon config ------------------------------------------------------------------

def test_daemon_config_loads_static_contacts(tmp_path, monkeypatch):
    monkeypatch.delenv(gd_config.ENV_BASE_URL, raising=False)
    monkeypatch.delenv(gd_config.ENV_API_KEY, raising=False)
    f = tmp_path / "gadgets.toml"
    f.write_text('[[contacts]]\nid = "aa:bb:cc:dd:ee:09"\n'
                 'protocol = "bthome_contact"\nname = "Freezer door"\n')
    cfg = gd_config.load(f)
    assert cfg.contacts[0]["id"] == "aa:bb:cc:dd:ee:09"
    cfg.contacts.append("junk")
    assert all(isinstance(d, dict) for d in cfg.validated().contacts)


# -- Pure helpers ------------------------------------------------------------------

def test_normalize_alarm_seconds():
    assert gadgets.normalize_alarm_seconds(300) == 300.0
    assert gadgets.normalize_alarm_seconds("120") == 120.0
    assert gadgets.normalize_alarm_seconds(-5) == 0.0
    assert gadgets.normalize_alarm_seconds(10 ** 9) == 86400.0
    assert gadgets.normalize_alarm_seconds(None) is None
    assert gadgets.normalize_alarm_seconds("x") is None


def test_normalize_contact_reading_shapes_and_rejects():
    r = gadgets.normalize_contact_reading(
        {"id": "aa:bb", "kind": "contact", "protocol": "bthome_contact",
         "name": "Freezer door", "open": True, "battery": 88, "rssi": -70}, 50.0)
    assert r["id"] == "AA:BB" and r["open"] is True and r["ts"] == 50.0
    assert r["battery"] == 88 and r["protocol"] == "bthome_contact"
    # No id or no boolean open state, no reading.
    assert gadgets.normalize_contact_reading({"open": True}, 0) is None
    assert gadgets.normalize_contact_reading({"id": "aa", "open": "yes"}, 0) is None
    assert gadgets.normalize_contact_reading({"id": "aa"}, 0) is None


def test_hygro_breach_matrix():
    th = {"min_temp_c": 0.0, "max_temp_c": 7.0,
          "min_humidity": 20.0, "max_humidity": 80.0}
    assert gadgets.hygro_breach(4.0, 50.0, th) is None
    assert gadgets.hygro_breach(9.0, 50.0, th)["kind"] == "temp_high"
    assert gadgets.hygro_breach(-2.0, 50.0, th)["kind"] == "temp_low"
    assert gadgets.hygro_breach(4.0, 90.0, th)["kind"] == "humidity_high"
    assert gadgets.hygro_breach(4.0, 10.0, th)["kind"] == "humidity_low"
    # A temperature breach outranks a humidity one.
    assert gadgets.hygro_breach(9.0, 90.0, th)["kind"] == "temp_high"
    # No reading never breaches (silence is the staleness alarm's job).
    assert gadgets.hygro_breach(None, None, th) is None
    assert gadgets.hygro_breach(9.0, None, {}) is None


def test_evaluate_protection_grace_persist_fire_once_and_clear():
    cond = {"hygro:AA": {"breach": {"kind": "temp_high", "value": 9, "limit": 7},
                         "grace": 300}}
    # Breach observed: quiet through the grace window.
    state, fired, cleared = gadgets.evaluate_protection(cond, {}, 1000.0)
    assert fired == [] and cleared == []
    assert state["hygro:AA"]["alarming"] is False
    # Still breaching at grace expiry: fires once.
    state, fired, _ = gadgets.evaluate_protection(cond, state, 1301.0)
    assert len(fired) == 1 and state["hygro:AA"]["alarming"] is True
    assert fired[0]["key"] == "hygro:AA"
    # Still breaching later: no second fire.
    state, fired, _ = gadgets.evaluate_protection(cond, state, 1400.0)
    assert fired == [] and state["hygro:AA"]["alarming"] is True
    # Recovered: the alarm clears and the state resets.
    healthy = {"hygro:AA": {"breach": None, "grace": 300}}
    state, fired, cleared = gadgets.evaluate_protection(healthy, state, 1500.0)
    assert cleared == ["hygro:AA"] and "hygro:AA" not in state
    # A fresh breach starts a fresh grace clock.
    state, fired, _ = gadgets.evaluate_protection(cond, state, 1600.0)
    assert fired == [] and state["hygro:AA"]["since"] == 1600.0


def test_evaluate_protection_since_override_and_blip():
    # A door open since 900 with a 180 s limit is already an alarm at 1100.
    cond = {"contact:AA": {"breach": {"kind": "open"}, "grace": 180, "since": 900.0}}
    state, fired, _ = gadgets.evaluate_protection(cond, {}, 1100.0)
    assert len(fired) == 1
    # A brief open (30 s so far) stays quiet: no defrost-blip paging.
    cond = {"contact:AA": {"breach": {"kind": "open"}, "grace": 180, "since": 1170.0}}
    state, fired, cleared = gadgets.evaluate_protection(cond, {}, 1200.0)
    assert fired == [] and state["contact:AA"]["alarming"] is False


def test_alarm_message_copy():
    msg = gadgets.alarm_message("hygro:AA", {"kind": "temp_high",
                                             "value": 9.0, "limit": 7.0},
                                "c", label="Fridge")
    assert "Fridge" in msg and "9°C" in msg and "7°C" in msg
    msg = gadgets.alarm_message("contact:AA", {"kind": "open"}, "f",
                                label="Freezer door", now=1240.0, since=1000.0)
    assert "Freezer door" in msg and "4 minutes" in msg
    msg = gadgets.alarm_message("hygro-stale:AA", {"kind": "stale"}, "f",
                                label="Fridge")
    assert "stopped reporting" in msg


# -- Ingest and sweep lifecycle ------------------------------------------------------

FRIDGE = "AA:BB:CC:DD:EE:01"
DOOR = "AA:BB:CC:DD:EE:09"


def _hygro_push(temp_c, humidity=50.0):
    return {"devices": [{"id": FRIDGE, "kind": "hygrometer", "name": "Fridge",
                         "protocol": "govee_hygro", "temp_c": temp_c,
                         "humidity": humidity, "battery": 90}]}


def _door_push(opened):
    return {"devices": [{"id": DOOR, "kind": "contact", "name": "Freezer door",
                         "protocol": "bthome_contact", "open": opened,
                         "battery": 77}]}


@pytest.fixture
def protected(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "gadget_devices", [], raising=False)
    monkeypatch.setattr(settings, "hygrometers_enabled", True, raising=False)
    monkeypatch.setattr(settings, "hygrometer_devices",
                        [{"id": FRIDGE, "name": "Fridge", "location": "Fridge",
                          "protocol": "govee_hygro",
                          "thresholds": {"max_temp_c": 7.0},
                          "alarm_grace_seconds": 120}], raising=False)
    monkeypatch.setattr(settings, "contacts_enabled", True, raising=False)
    monkeypatch.setattr(settings, "contact_devices",
                        [{"id": DOOR, "name": "Freezer door",
                          "location": "Freezer",
                          "protocol": "bthome_contact"}], raising=False)
    return tmp_path


def _warning_events():
    return [e for e in ha_events.poll(0)["events"] if e.get("type") == "warning"]


def test_hygro_threshold_alarm_fires_after_grace_and_clears(protected):
    t0 = 10_000.0
    gadgets.ingest(_hygro_push(9.0), now=t0)          # breach begins
    assert _warning_events() == []                    # inside the grace
    gadgets.run_protection_sweep(now=t0 + 60)
    assert _warning_events() == []
    gadgets.run_protection_sweep(now=t0 + 121)        # grace elapsed
    events = _warning_events()
    assert len(events) == 1
    assert events[0]["key"] == f"gadget-alarm:hygro:{FRIDGE}"
    assert events[0]["level"] == "error"
    assert "Fridge" in events[0]["message"]
    # The state carries the alarm for every surface.
    dev = gadgets.get_state(now=t0 + 130)["hygrometers"][0]
    assert dev["alarming"] is True and "limit" in dev["alarm_message"]
    alarms = gadgets.active_alarms(now=t0 + 130)
    assert alarms[0]["kind"] == "hygrometer" and alarms[0]["device_id"] == FRIDGE
    assert alarms[0]["started_epoch"] == int(t0 + 121)
    # Still alarming: no duplicate warning (edge-triggered).
    gadgets.run_protection_sweep(now=t0 + 200)
    assert len(_warning_events()) == 1
    # Recovery clears the alarm and re-arms it.
    gadgets.ingest(_hygro_push(4.0), now=t0 + 300)
    assert gadgets.active_alarms(now=t0 + 300) == []
    assert gadgets.get_state(now=t0 + 300)["hygrometers"][0]["alarming"] is False
    gadgets.ingest(_hygro_push(9.5), now=t0 + 400)
    gadgets.run_protection_sweep(now=t0 + 521)
    assert len(_warning_events()) == 2                # a genuine new alarm


def test_door_open_too_long_alarm_lifecycle(protected):
    t0 = 20_000.0
    gadgets.ingest(_door_push(True), now=t0)
    state = gadgets.get_state(now=t0 + 30)
    door = state["contacts"][0]
    assert door["open"] is True and door["open_seconds"] == 30.0
    assert door["open_alarm_seconds"] == 180
    assert _warning_events() == []                    # not open long enough yet
    # The periodic sweep alarms even with no new advertisement: open_since
    # rode the reading, so duration measures from the real opening.
    gadgets.run_protection_sweep(now=t0 + 181)
    events = _warning_events()
    assert len(events) == 1
    assert events[0]["key"] == f"gadget-alarm:contact:{DOOR}"
    assert "Freezer door" in events[0]["message"]
    assert gadgets.get_state(now=t0 + 200)["contacts"][0]["alarming"] is True
    # Repeated open pushes do not restart the clock or re-fire.
    gadgets.ingest(_door_push(True), now=t0 + 240)
    assert len(_warning_events()) == 1
    # Closing clears everything.
    gadgets.ingest(_door_push(False), now=t0 + 300)
    assert gadgets.active_alarms(now=t0 + 300) == []
    door = gadgets.get_state(now=t0 + 310)["contacts"][0]
    assert door["open"] is False and door["alarming"] is False


def test_stale_alarm_is_off_by_default_and_fires_when_set(protected, monkeypatch):
    t0 = 30_000.0
    gadgets.ingest(_hygro_push(4.0), now=t0)
    # Default: a silent sensor never raises the not-reporting alarm.
    gadgets.run_protection_sweep(now=t0 + 7200)
    assert _warning_events() == []
    # Opt in with a 10 minute window.
    devices = [dict(settings.hygrometer_devices[0], stale_alarm_seconds=600)]
    monkeypatch.setattr(settings, "hygrometer_devices", devices, raising=False)
    gadgets.run_protection_sweep(now=t0 + 500)
    assert _warning_events() == []
    gadgets.run_protection_sweep(now=t0 + 601)
    events = _warning_events()
    assert len(events) == 1 and events[0]["level"] == "warning"
    assert "stopped reporting" in events[0]["message"]
    # A fresh reading clears it.
    gadgets.ingest(_hygro_push(4.0), now=t0 + 700)
    assert gadgets.active_alarms(now=t0 + 700) == []


def test_disabled_classes_never_alarm(protected, monkeypatch):
    t0 = 40_000.0
    gadgets.ingest(_hygro_push(9.0), now=t0)
    gadgets.ingest(_door_push(True), now=t0)
    monkeypatch.setattr(settings, "hygrometers_enabled", False, raising=False)
    monkeypatch.setattr(settings, "contacts_enabled", False, raising=False)
    gadgets.run_protection_sweep(now=t0 + 3600)
    assert _warning_events() == []


def test_contact_discovery_has_its_own_list(protected):
    gadgets.ingest({"devices": [], "discovered": [
        {"id": "11:22:33:44:55:09", "name": "Shelly BLU DW",
         "protocol": "bthome_contact", "kind": "contact", "rssi": -66},
    ]})
    state = gadgets.get_state()
    assert [d["id"] for d in state["contact_discovered"]] == ["11:22:33:44:55:09"]
    assert state["discovered"] == [] and state["hygro_discovered"] == []


# -- Endpoints: registry CRUD ---------------------------------------------------------

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
    monkeypatch.setattr(settings, "hygrometers_enabled", False, raising=False)
    monkeypatch.setattr(settings, "hygrometer_devices", [], raising=False)
    monkeypatch.setattr(settings, "contacts_enabled", False, raising=False)
    monkeypatch.setattr(settings, "contact_devices", [], raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_add_contact_enables_class_and_round_trips(client):
    r = client.post("/gadgets/contacts", json={
        "id": "aa:bb:cc:dd:ee:09", "name": "Freezer door",
        "protocol": "bthome_contact", "location": "Freezer"}).json()
    assert r["ok"] is True
    cfg = client.get("/gadgets/config").json()
    assert cfg["contacts_enabled"] is True
    dev = cfg["contacts"][0]
    assert dev["id"] == "AA:BB:CC:DD:EE:09" and dev["location"] == "Freezer"
    # The other classes are untouched.
    assert cfg["enabled"] is False and cfg["hygrometers_enabled"] is False
    # Re-adding updates rather than duplicating.
    client.post("/gadgets/contacts", json={"id": "AA:BB:CC:DD:EE:09",
                                           "name": "Garage freezer door"})
    contacts = client.get("/gadgets/config").json()["contacts"]
    assert len(contacts) == 1 and contacts[0]["name"] == "Garage freezer door"


def test_contact_edit_and_remove(client):
    client.post("/gadgets/contacts", json={"id": "AA:BB:CC:DD:EE:09",
                                           "protocol": "switchbot_contact"})
    r = client.post("/gadgets/contacts/edit", json={
        "device_id": "aa:bb:cc:dd:ee:09", "name": "Fridge door",
        "location": "Fridge", "open_alarm_seconds": 300}).json()
    assert r["ok"] is True
    dev = client.get("/gadgets/config").json()["contacts"][0]
    assert dev["name"] == "Fridge door" and dev["open_alarm_seconds"] == 300
    # Null restores the default; only present fields apply.
    client.post("/gadgets/contacts/edit", json={
        "device_id": "AA:BB:CC:DD:EE:09", "open_alarm_seconds": None})
    dev = client.get("/gadgets/config").json()["contacts"][0]
    assert "open_alarm_seconds" not in dev and dev["name"] == "Fridge door"
    r = client.post("/gadgets/contacts/edit",
                    json={"device_id": "no:such", "name": "X"}).json()
    assert r["ok"] is False
    assert client.delete("/gadgets/contacts/aa:bb:cc:dd:ee:09").json()["ok"] is True
    assert client.get("/gadgets/config").json()["contacts"] == []


def test_hygrometer_edit_alarm_fields_round_trip(client):
    client.post("/gadgets/hygrometers", json={"id": "AA:BB:CC:DD:EE:01",
                                              "protocol": "govee_hygro"})
    r = client.post("/gadgets/hygrometers/edit", json={
        "device_id": "AA:BB:CC:DD:EE:01", "max_temp_c": 7.0,
        "alarm_grace_seconds": 600, "stale_alarm_seconds": 900}).json()
    assert r["ok"] is True
    dev = client.get("/gadgets/config").json()["hygrometers"][0]
    assert dev["alarm_grace_seconds"] == 600 and dev["stale_alarm_seconds"] == 900
    # Null falls back to the class default (grace) / off (stale).
    client.post("/gadgets/hygrometers/edit", json={
        "device_id": "AA:BB:CC:DD:EE:01", "alarm_grace_seconds": None,
        "stale_alarm_seconds": 0})
    dev = client.get("/gadgets/config").json()["hygrometers"][0]
    assert "alarm_grace_seconds" not in dev and dev["stale_alarm_seconds"] == 0
    # A rename never touches the alarm fields or thresholds.
    client.post("/gadgets/hygrometers/edit", json={
        "device_id": "AA:BB:CC:DD:EE:01", "name": "Fridge"})
    dev = client.get("/gadgets/config").json()["hygrometers"][0]
    assert dev["thresholds"] == {"max_temp_c": 7.0}


def test_state_carries_contact_block_and_defaults(client):
    client.post("/gadgets/contacts", json={
        "id": "AA:BB:CC:DD:EE:09", "name": "Freezer door",
        "protocol": "bthome_contact", "location": "Freezer"})
    client.post("/gadgets/readings", json=_door_push(True))
    state = client.get("/gadgets/state").json()
    assert state["contacts_enabled"] is True
    dev = state["contacts"][0]
    assert dev["open"] is True and dev["open_alarm_seconds"] == 180
    assert dev["battery"] == 77 and dev["alarming"] is False


def test_gadgets_pane_renders_door_sensor_section(client, monkeypatch):
    from unittest.mock import patch
    with patch.object(type(settings), "is_configured", lambda self: True):
        html = client.get("/setup").text
    for control in ("contacts_enabled", "contact-add-id", "contact-add-name",
                    "contact-add-location", "contact-add-protocol",
                    "cub_alerts_take_over"):
        assert f'id="{control}"' in html, control
    assert "Door sensors" in html


# -- Cub escalation ---------------------------------------------------------------------

_ALARM = {"kind": "contact", "device_id": "AA", "name": "Freezer door",
          "location": "Freezer", "message": "Freezer door has been open for 4 minutes.",
          "started_epoch": 1_700_000_000}


def test_cub_alerts_block_is_pure_and_calm():
    rows = cub_svc.alerts_block([_ALARM, "junk", {}])
    assert rows[0] == {"kind": "contact", "id": "AA", "name": "Freezer door",
                       "location": "Freezer",
                       "message": "Freezer door has been open for 4 minutes.",
                       "started_epoch": 1_700_000_000}
    assert rows[1]["kind"] == "" and rows[1]["started_epoch"] == 0
    assert cub_svc.alerts_block([]) == []
    assert cub_svc.alerts_block(None) == []


def _merged(**over):
    base = {"default_view": "expiring", "timers_take_over": True,
            "probes_take_over": True, "alerts_take_over": True,
            "rotation": [], "rotate_seconds": 12, "poll_seconds": 15}
    base.update(over)
    return base


def test_decide_view_alert_outranks_everything():
    timer = {"id": 1, "label": "Pasta", "deadline_epoch": 1.0, "expired": False}
    probe = {"id": "X", "probe": 1, "temp_c": 60.0, "target_c": 74.0}
    alerts = [cub_svc.alerts_block([_ALARM])[0]]
    assert cub_svc.decide_view([timer], [probe], _merged(), alerts) == "alert"
    # The takeover toggle turns escalation off; timers win again.
    assert cub_svc.decide_view([timer], [probe],
                               _merged(alerts_take_over=False), alerts) == "timers"
    # No alarms, no takeover: existing behavior is untouched.
    assert cub_svc.decide_view([timer], [probe], _merged(), []) == "timers"
    assert cub_svc.decide_view([], [], _merged(), None) == "expiring"


def test_cub_summary_escalates_on_live_alarm(client, monkeypatch):
    monkeypatch.setattr(settings, "contacts_enabled", True, raising=False)
    monkeypatch.setattr(settings, "contact_devices",
                        [{"id": DOOR, "name": "Freezer door",
                          "location": "Freezer",
                          "protocol": "bthome_contact"}], raising=False)
    # Door opened 10 minutes ago (real clock: the summary evaluates at now).
    t_open = time.time() - 600
    gadgets.ingest(_door_push(True), now=t_open)
    gadgets.run_protection_sweep()
    body = client.get("/cub/summary").json()
    assert body["view"] == "alert"
    assert body["settings"]["alerts_take_over"] is True
    alert = body["alerts"][0]
    assert alert["kind"] == "contact" and alert["name"] == "Freezer door"
    assert "open" in alert["message"] and alert["started_epoch"] > 0
    # With the takeover off the alerts block still reports, calmly.
    monkeypatch.setattr(settings, "cub_alerts_take_over", False, raising=False)
    body = client.get("/cub/summary").json()
    assert body["view"] != "alert" and body["alerts"] != []
