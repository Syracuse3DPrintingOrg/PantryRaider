"""BLE hygrometer support (FoodAssistant-q97i).

Hygrometers (Govee H5075/H5074, Xiaomi LYWSD03MMC on the community ATC
firmware, SwitchBot Meter, Inkbird IBS-TH) are a separate device class from
the cooking-probe thermometers: ambient temperature + humidity for a fridge,
freezer, pantry, or room. Covers the pure advertisement decoders (vectors
built from the documented formats, negative temperatures included), the
ingest kind-routing, the registry CRUD with the stored threshold fields, the
Home Assistant and ESPHome sources, the Time & Temp page render, and the
Cub summary block. No radio, bluez, or bleak needed.
"""
from __future__ import annotations

import json
import os
import struct
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from foodassistant_gadgets import decoders  # noqa: E402
from foodassistant_gadgets import config as gd_config  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import gadgets, gadgets_ha, gadgets_esp  # noqa: E402
from app.services import cub as cub_svc  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    gadgets.reset()
    yield
    gadgets.reset()


# -- Govee H5075 / H5074 decoder ----------------------------------------------

def test_govee_h5075_decodes_packed_reading():
    # The documented GoveeWatcher example: 0x03215A packs 20.5 C / 14.6 %,
    # battery 100. The /10000 read keeps the humidity digits in the tail, the
    # same as the govee-ble parser.
    payload = bytes([0x00, 0x03, 0x21, 0x5A, 0x64, 0x00])
    r = decoders.decode_govee_hygrometer(payload)
    assert r == {"temp_c": 20.51, "humidity_pct": 14.6, "battery_pct": 100}


def test_govee_h5075_negative_temperature_sign_bit():
    # -5.2 C at 45.0 %: base = 52*1000 + 450 with the 0x800000 sign bit set.
    base = 52 * 1000 + 450
    raw = (0x800000 | base).to_bytes(3, "big")
    r = decoders.decode_govee_hygrometer(bytes([0x00]) + raw + bytes([0x40, 0x00]))
    assert r["temp_c"] == pytest.approx(-5.25, abs=0.02)
    assert r["humidity_pct"] == 45.0 and r["battery_pct"] == 64


def test_govee_h5074_le_hundredths():
    # -10.5 C, 44.82 %, battery 100: little-endian hundredths.
    payload = bytes([0x00]) + struct.pack("<hHB", -1050, 4482, 100) + bytes([0x02])
    r = decoders.decode_govee_hygrometer(payload)
    assert r == {"temp_c": -10.5, "humidity_pct": 44.8, "battery_pct": 100}


def test_govee_hygro_rejects_unknown_lengths():
    assert decoders.decode_govee_hygrometer(b"") is None
    assert decoders.decode_govee_hygrometer(bytes(5)) is None
    assert decoders.decode_govee_hygrometer(bytes(14)) is None  # a grill frame


# -- Xiaomi ATC firmware decoder (atc1441 and pvvx) -----------------------------

def test_atc1441_frame_is_big_endian_tenths():
    mac = bytes.fromhex("A4C138AABBCC")
    # -5.5 C, 43 %, battery 87 %, 2900 mV, counter 7.
    frame = mac + (-55).to_bytes(2, "big", signed=True) + bytes([43, 87]) \
        + (2900).to_bytes(2, "big") + bytes([7])
    assert len(frame) == 13
    r = decoders.decode_atc_advertisement(frame)
    assert r == {"temp_c": -5.5, "humidity_pct": 43.0, "battery_pct": 87}


def test_pvvx_frame_is_little_endian_hundredths():
    mac = bytes.fromhex("CCBBAA38C1A4")
    # -5.5 C, 43.2 %, 2900 mV, battery 87 %, counter, flags.
    frame = mac + struct.pack("<hH", -550, 4320) + (2900).to_bytes(2, "little") \
        + bytes([87, 7, 0])
    assert len(frame) == 15
    r = decoders.decode_atc_advertisement(frame)
    assert r == {"temp_c": -5.5, "humidity_pct": 43.2, "battery_pct": 87}


def test_atc_rejects_other_lengths():
    assert decoders.decode_atc_advertisement(bytes(12)) is None
    assert decoders.decode_atc_advertisement(bytes(16)) is None


# -- SwitchBot Meter decoder -----------------------------------------------------

def test_switchbot_meter_positive_and_negative():
    # 22.2 C / 45 %: byte 4's top bit SET means at-or-above zero.
    payload = bytes([0x54, 0x00, 90, 0x02, 0x80 | 22, 45])
    r = decoders.decode_switchbot_meter(payload)
    assert r == {"temp_c": 22.2, "humidity_pct": 45.0, "battery_pct": 90}
    # The freezer: -8.2 C (top bit clear).
    payload = bytes([0x69, 0x00, 90, 0x02, 22, 45])
    assert decoders.decode_switchbot_meter(payload)["temp_c"] == -22.2


def test_switchbot_rejects_non_meter_device_types():
    # 0x48 'H' is a Bot, not a meter.
    assert decoders.decode_switchbot_meter(bytes([0x48, 0, 90, 0, 0x80 | 20, 40])) is None
    assert decoders.decode_switchbot_meter(b"\x54\x00") is None


# -- Inkbird IBS-TH decoder --------------------------------------------------------

def test_inkbird_hygro_restores_company_id():
    # -12.34 C / 56.78 %, battery 77. The first two frame bytes ride in the
    # manufacturer company id, exactly like the TempSpike.
    frame = struct.pack("<hH", -1234, 5678) + bytes([0, 0, 0, 77, 0])
    company_id = int.from_bytes(frame[:2], "little")
    r = decoders.decode_inkbird_hygro_from_manufacturer({company_id: frame[2:]})
    assert r == {"temp_c": -12.34, "humidity_pct": 56.8, "battery_pct": 77}


def test_inkbird_temp_only_model_has_no_humidity():
    frame = struct.pack("<hH", 2150, 0) + bytes([0, 0, 0, 50, 0])
    r = decoders.decode_inkbird_hygrometer(frame)
    assert r["temp_c"] == 21.5 and r["humidity_pct"] is None


def test_inkbird_rejects_wrong_length():
    assert decoders.decode_inkbird_hygrometer(bytes(8)) is None
    assert decoders.decode_inkbird_hygro_from_manufacturer(None) is None


# -- Identification -------------------------------------------------------------

GOVEE_75 = bytes([0x00, 0x03, 0x21, 0x5A, 0x64, 0x00])


def test_identify_hygrometer_by_manufacturer_and_service_data():
    assert decoders.identify_hygrometer(
        None, {decoders.GOVEE_HYGROMETER_MANUFACTURER_ID: GOVEE_75}) == "govee_hygro"
    assert decoders.identify_hygrometer(
        "ATC_AABBCC") == "xiaomi_atc"
    assert decoders.identify_hygrometer(
        None, None, {decoders.ATC_SERVICE_UUID: bytes(13)}) == "xiaomi_atc"
    assert decoders.identify_hygrometer(
        None, None,
        {"0000fd3d-0000-1000-8000-00805f9b34fb": bytes([0x54, 0, 90, 0, 0x80 | 20, 40])}
    ) == "switchbot_meter"
    assert decoders.identify_hygrometer("sps") == "inkbird_hygro"
    assert decoders.identify_hygrometer("GVH5075_1234") == "govee_hygro"
    assert decoders.identify_hygrometer("TP960R") is None
    assert decoders.identify_hygrometer(None) is None


def test_identify_hygrometer_never_matches_a_govee_grill():
    # An H5182 grill frame (14 bytes, 0x01 marker) is a thermometer, not a
    # hygrometer, even with no name.
    grill = bytes([0x82, 0x01]) + bytes(12)
    md = {0x9A21: grill}
    assert decoders.identify_hygrometer(None, md) is None
    assert decoders.identify(None, md) == decoders.PROTOCOL_GOVEE_GRILL


def test_thermometer_identify_still_excludes_hygrometers():
    assert decoders.identify(
        "GVH5075_1234",
        {decoders.GOVEE_HYGROMETER_MANUFACTURER_ID: GOVEE_75}) is None


def test_decode_hygrometer_dispatch():
    r = decoders.decode_hygrometer(
        "govee_hygro", {decoders.GOVEE_HYGROMETER_MANUFACTURER_ID: GOVEE_75})
    assert r["temp_c"] == 20.51
    r = decoders.decode_hygrometer(
        "switchbot_meter", None,
        {"0000fd3d-0000-1000-8000-00805f9b34fb": bytes([0x54, 0, 90, 2, 0x80 | 4, 62])})
    assert r == {"temp_c": 4.2, "humidity_pct": 62.0, "battery_pct": 90}
    assert decoders.decode_hygrometer("bogus", {}, {}) is None


# -- Daemon config -----------------------------------------------------------------

def test_daemon_config_loads_static_hygrometers(tmp_path, monkeypatch):
    monkeypatch.delenv(gd_config.ENV_BASE_URL, raising=False)
    monkeypatch.delenv(gd_config.ENV_API_KEY, raising=False)
    f = tmp_path / "gadgets.toml"
    f.write_text('[[hygrometers]]\nid = "aa:bb:cc:dd:ee:01"\n'
                 'protocol = "govee_hygro"\nname = "Fridge"\n')
    cfg = gd_config.load(f)
    assert cfg.hygrometers[0]["id"] == "aa:bb:cc:dd:ee:01"
    # Junk entries are dropped by validation.
    cfg.hygrometers.append("nope")
    assert all(isinstance(d, dict) for d in cfg.validated().hygrometers)


# -- Normalization and ingest kind-routing --------------------------------------------

def test_normalize_hygro_reading_shapes_and_rejects():
    r = gadgets.normalize_hygro_reading(
        {"id": "aa:bb", "kind": "hygrometer", "protocol": "govee_hygro",
         "name": "Fridge", "temp_c": 3.456, "humidity": 51.23,
         "battery": 88, "rssi": -70}, 100.0)
    assert r["id"] == "AA:BB" and r["temp_c"] == 3.46
    assert r["humidity"] == 51.2 and r["battery"] == 88 and r["ts"] == 100.0
    # No temperature, no reading; out-of-band values clamp or clear.
    assert gadgets.normalize_hygro_reading({"id": "aa", "humidity": 50}, 0) is None
    assert gadgets.normalize_hygro_reading({"id": "aa", "temp_c": 400}, 0) is None
    r = gadgets.normalize_hygro_reading(
        {"id": "aa", "temp_c": -20.0, "humidity": 150, "battery": 400}, 0)
    assert r["humidity"] is None and r["battery"] == 100


def test_normalize_hygro_thresholds_round_trip_and_junk():
    clean = gadgets.normalize_hygro_thresholds(
        {"min_temp_c": 1.0, "max_temp_c": 5.54, "min_humidity": 20,
         "max_humidity": 70, "bogus": 1})
    assert clean == {"min_temp_c": 1.0, "max_temp_c": 5.5,
                     "min_humidity": 20.0, "max_humidity": 70.0}
    assert gadgets.normalize_hygro_thresholds(None) == {}
    assert gadgets.normalize_hygro_thresholds(
        {"min_temp_c": "x", "max_humidity": 200}) == {}


def _hygro_push(temp_c=4.0, humidity=48.0, dev_id="AA:BB:CC:DD:EE:01", **kw):
    return {"devices": [{
        "id": dev_id, "kind": "hygrometer", "name": "Fridge sensor",
        "protocol": "govee_hygro", "temp_c": temp_c, "humidity": humidity,
        "battery": kw.get("battery", 90), "rssi": kw.get("rssi", -65),
    }], "discovered": kw.get("discovered", [])}


def test_ingest_routes_hygrometers_and_survives_worker_swap(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "hygrometers_enabled", True, raising=False)
    monkeypatch.setattr(settings, "hygrometer_devices",
                        [{"id": "AA:BB:CC:DD:EE:01", "name": "Fridge",
                          "location": "Fridge", "protocol": "govee_hygro",
                          "thresholds": {"max_temp_c": 7.0}}], raising=False)
    monkeypatch.setattr(settings, "gadget_devices", [], raising=False)
    out = gadgets.ingest(_hygro_push())
    assert out["ok"] is True
    # A hygrometer never lands in the thermometer map.
    state = gadgets.get_state()
    assert state["devices"] == []
    # The state survives a "different worker": wipe the cache, re-read.
    gadgets._mtime = None
    gadgets._state = {"devices": {}, "discovered": {}, "alerts": {}}
    state = gadgets.get_state()
    dev = state["hygrometers"][0]
    assert dev["id"] == "AA:BB:CC:DD:EE:01" and dev["location"] == "Fridge"
    assert dev["temp_c"] == 4.0 and dev["humidity"] == 48.0
    assert dev["battery"] == 90 and dev["stale"] is False
    assert dev["thresholds"] == {"max_temp_c": 7.0}
    raw = json.loads((tmp_path / "gadgets.json").read_text())
    assert "AA:BB:CC:DD:EE:01" in raw["hygrometers"]


def test_discovered_hygrometers_have_their_own_list(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "hygrometer_devices", [], raising=False)
    monkeypatch.setattr(settings, "gadget_devices", [], raising=False)
    gadgets.ingest({"devices": [], "discovered": [
        {"id": "11:22:33:44:55:01", "name": "GVH5075_1234",
         "protocol": "govee_hygro", "kind": "hygrometer", "rssi": -72},
        {"id": "11:22:33:44:55:02", "name": "iBBQ", "protocol": "inkbird"},
    ]})
    state = gadgets.get_state()
    assert [d["id"] for d in state["hygro_discovered"]] == ["11:22:33:44:55:01"]
    assert [d["id"] for d in state["discovered"]] == ["11:22:33:44:55:02"]
    # Once configured, it leaves the hygro add list.
    monkeypatch.setattr(settings, "hygrometer_devices",
                        [{"id": "11:22:33:44:55:01", "protocol": "govee_hygro"}],
                        raising=False)
    assert gadgets.get_state()["hygro_discovered"] == []


def test_hygro_stale_window_is_calmer_than_probes(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "hygrometer_devices",
                        [{"id": "AA:BB:CC:DD:EE:01", "protocol": "govee_hygro"}],
                        raising=False)
    gadgets.ingest(_hygro_push(), now=1000.0)
    # Two minutes later a probe would be stale; a fridge sensor is not yet.
    dev = gadgets.get_state(now=1000.0 + 120)["hygrometers"][0]
    assert dev["stale"] is False
    dev = gadgets.get_state(now=1000.0 + gadgets.HYGRO_STALE_SECONDS + 1)["hygrometers"][0]
    assert dev["stale"] is True


# -- Endpoints: registry CRUD with thresholds ------------------------------------------

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
    monkeypatch.setattr(settings, "gadget_ha_enabled", False, raising=False)
    monkeypatch.setattr(settings, "gadget_ha_hygrometers", [], raising=False)
    monkeypatch.setattr(settings, "gadget_esp_enabled", False, raising=False)
    monkeypatch.setattr(settings, "gadget_esp_devices", [], raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_add_hygrometer_enables_class_and_round_trips(client):
    r = client.post("/gadgets/hygrometers", json={
        "id": "aa:bb:cc:dd:ee:01", "name": "Fridge", "protocol": "govee_hygro",
        "location": "Fridge"}).json()
    assert r["ok"] is True
    cfg = client.get("/gadgets/config").json()
    assert cfg["hygrometers_enabled"] is True
    dev = cfg["hygrometers"][0]
    assert dev["id"] == "AA:BB:CC:DD:EE:01" and dev["location"] == "Fridge"
    # The thermometer class is untouched.
    assert cfg["enabled"] is False and cfg["devices"] == []
    # Re-adding updates rather than duplicating.
    client.post("/gadgets/hygrometers", json={"id": "AA:BB:CC:DD:EE:01",
                                              "name": "Garage fridge"})
    hygros = client.get("/gadgets/config").json()["hygrometers"]
    assert len(hygros) == 1 and hygros[0]["name"] == "Garage fridge"


def test_hygrometer_edit_name_location_thresholds(client):
    client.post("/gadgets/hygrometers", json={"id": "AA:BB:CC:DD:EE:01",
                                              "protocol": "govee_hygro"})
    r = client.post("/gadgets/hygrometers/edit", json={
        "device_id": "aa:bb:cc:dd:ee:01", "name": "Freezer",
        "location": "Freezer", "min_temp_c": -25.0, "max_temp_c": -15.0,
        "max_humidity": 80.0}).json()
    assert r["ok"] is True
    dev = client.get("/gadgets/config").json()["hygrometers"][0]
    assert dev["name"] == "Freezer" and dev["location"] == "Freezer"
    assert dev["thresholds"] == {"min_temp_c": -25.0, "max_temp_c": -15.0,
                                 "max_humidity": 80.0}
    # Only the fields present apply: an explicit null clears one threshold,
    # a location tweak leaves the rest alone.
    client.post("/gadgets/hygrometers/edit", json={
        "device_id": "AA:BB:CC:DD:EE:01", "max_humidity": None})
    dev = client.get("/gadgets/config").json()["hygrometers"][0]
    assert dev["thresholds"] == {"min_temp_c": -25.0, "max_temp_c": -15.0}
    client.post("/gadgets/hygrometers/edit", json={
        "device_id": "AA:BB:CC:DD:EE:01", "location": "Garage"})
    dev = client.get("/gadgets/config").json()["hygrometers"][0]
    assert dev["location"] == "Garage"
    assert dev["thresholds"] == {"min_temp_c": -25.0, "max_temp_c": -15.0}
    # Unknown device reports, never raises.
    r = client.post("/gadgets/hygrometers/edit",
                    json={"device_id": "no:such", "name": "X"}).json()
    assert r["ok"] is False


def test_remove_hygrometer(client):
    client.post("/gadgets/hygrometers", json={"id": "AA:BB:CC:DD:EE:01"})
    assert client.delete("/gadgets/hygrometers/aa:bb:cc:dd:ee:01").json()["ok"] is True
    assert client.get("/gadgets/config").json()["hygrometers"] == []


def test_readings_push_with_kind_routes_to_hygro_state(client):
    client.post("/gadgets/hygrometers", json={
        "id": "AA:BB:CC:DD:EE:01", "name": "Fridge", "protocol": "govee_hygro",
        "location": "Fridge"})
    client.post("/gadgets/readings", json=_hygro_push(3.9, 52.0))
    state = client.get("/gadgets/state").json()
    assert state["hygrometers_enabled"] is True
    dev = state["hygrometers"][0]
    assert dev["name"] == "Fridge" and dev["temp_c"] == 3.9
    assert dev["humidity"] == 52.0 and dev["stale"] is False
    # Thermometer devices stay empty: no cross-contamination.
    assert state["devices"] == []


def test_ha_hygrometer_pair_add_and_remove(client):
    r = client.post("/gadgets/ha-hygrometers", json={
        "temperature": "sensor.fridge_temperature",
        "humidity": "sensor.fridge_humidity", "name": "Fridge",
        "location": "Fridge"}).json()
    assert r["ok"] is True
    assert r["pairs"][0]["temperature"] == "sensor.fridge_temperature"
    dev = client.get("/gadgets/config").json()["hygrometers"][0]
    assert dev["id"] == "HAH:SENSOR.FRIDGE_TEMPERATURE"
    assert dev["protocol"] == "home_assistant" and dev["location"] == "Fridge"
    # Removing the device also drops the polled pair.
    client.delete("/gadgets/hygrometers/HAH:SENSOR.FRIDGE_TEMPERATURE")
    assert client.get("/gadgets/config").json()["hygrometers"] == []
    assert gadgets_ha.configured_hygro_pairs() == []


def test_ha_hygrometer_pair_validation(client):
    assert client.post("/gadgets/ha-hygrometers",
                       json={"temperature": "nope"}).json()["ok"] is False
    assert client.post("/gadgets/ha-hygrometers", json={
        "temperature": "sensor.ok", "humidity": "not an id"}).json()["ok"] is False


def test_esp_hygrometer_joins_hygro_class(client):
    r = client.post("/gadgets/esp-devices", json={
        "host": "192.168.1.50", "sensor": "fridge_temp", "name": "Fridge",
        "kind": "hygrometer", "humidity": "fridge_humidity",
        "location": "Fridge"}).json()
    assert r["ok"] is True
    assert r["esp_devices"][0]["kind"] == "hygrometer"
    assert r["esp_devices"][0]["humidity"] == "fridge_humidity"
    cfg = client.get("/gadgets/config").json()
    assert cfg["hygrometers"][0]["id"] == "ESP:192.168.1.50:FRIDGE_TEMP"
    # Not a cooking probe.
    assert cfg["devices"] == []
    client.delete("/gadgets/hygrometers/ESP:192.168.1.50:FRIDGE_TEMP")
    assert client.get("/gadgets/config").json()["hygrometers"] == []
    assert gadgets_esp.configured_devices() == []


def test_timers_page_renders_hygro_section(client):
    html = client.get("/ui/timers").text
    assert "hygroSection" in html and "hygroGrid" in html


# -- Home Assistant source parsing -----------------------------------------------------

def test_ha_hygro_reading_pairs_temp_and_humidity():
    pair = {"temperature": "sensor.fridge_temperature",
            "humidity": "sensor.fridge_humidity", "name": "Fridge"}
    temp_data = {"state": "38.5", "attributes": {
        "unit_of_measurement": "°F", "friendly_name": "Fridge temp"}}
    hum_data = {"state": "51.4", "attributes": {"battery_level": 77}}
    r = gadgets_ha.hygro_reading(pair, temp_data, hum_data, now=0)
    assert r["kind"] == "hygrometer" and r["protocol"] == "home_assistant"
    assert r["id"] == "HAH:SENSOR.FRIDGE_TEMPERATURE"
    assert r["temp_c"] == pytest.approx(3.61, abs=0.01)
    assert r["humidity"] == 51.4 and r["battery"] == 77
    # An unusable humidity companion degrades to None, never drops the reading.
    r = gadgets_ha.hygro_reading(pair, temp_data, {"state": "unavailable"}, now=0)
    assert r["humidity"] is None
    # No usable temperature, no reading.
    assert gadgets_ha.hygro_reading(pair, {"state": "unknown"}, hum_data, now=0) is None


def test_ha_configured_hygro_pairs_sanitizes():
    class S:  # a settings stand-in via monkeypatching would touch the real one
        pass
    import app.config as cfg
    saved = cfg.settings.gadget_ha_hygrometers
    try:
        cfg.settings.gadget_ha_hygrometers = [
            {"temperature": "sensor.fridge_t", "humidity": "sensor.fridge_h"},
            {"temperature": "sensor.fridge_t"},          # duplicate: dropped
            {"temperature": "bad id"},                    # invalid: dropped
            "junk",
            {"temperature": "sensor.room_t", "humidity": "bad"},
        ]
        pairs = gadgets_ha.configured_hygro_pairs()
        assert [p["temperature"] for p in pairs] == ["sensor.fridge_t", "sensor.room_t"]
        assert pairs[0]["humidity"] == "sensor.fridge_h"
        assert pairs[1]["humidity"] == ""   # invalid companion cleared
    finally:
        cfg.settings.gadget_ha_hygrometers = saved


def test_is_humidity_entity():
    assert gadgets_ha.is_humidity_entity({
        "entity_id": "sensor.fridge_humidity",
        "attributes": {"device_class": "humidity"}}) is True
    assert gadgets_ha.is_humidity_entity({
        "entity_id": "sensor.fridge_humidity",
        "attributes": {"unit_of_measurement": "%"}}) is True
    # A percent unit alone (a battery, a disk) is not a humidity sensor.
    assert gadgets_ha.is_humidity_entity({
        "entity_id": "sensor.disk_use",
        "attributes": {"unit_of_measurement": "%"}}) is False
    assert gadgets_ha.is_humidity_entity({"entity_id": "light.kitchen"}) is False


# -- ESPHome source parsing ------------------------------------------------------------

def test_esp_hygro_sensor_reading():
    temp_body = {"id": "sensor-fridge_temp", "value": 3.8, "state": "3.8 °C"}
    hum_body = {"id": "sensor-fridge_humidity", "value": 55.4, "state": "55.4 %"}
    r = gadgets_esp.hygro_sensor_reading("fridge.local", "fridge_temp",
                                         temp_body, hum_body, now=0, battery=66)
    assert r["kind"] == "hygrometer" and r["protocol"] == "esphome"
    assert r["id"] == "ESP:FRIDGE.LOCAL:FRIDGE_TEMP"
    assert r["temp_c"] == 3.8 and r["humidity"] == 55.4 and r["battery"] == 66
    # A missing companion degrades to humidity None.
    r = gadgets_esp.hygro_sensor_reading("fridge.local", "fridge_temp",
                                         temp_body, None, now=0)
    assert r["humidity"] is None
    # No temperature, no reading.
    assert gadgets_esp.hygro_sensor_reading(
        "fridge.local", "fridge_temp", {"value": float("nan")}, hum_body, now=0) is None


def test_esp_humidity_from_rejects_junk():
    assert gadgets_esp.humidity_from(55.44, "55.4 %") == 55.4
    assert gadgets_esp.humidity_from("x", "") is None
    assert gadgets_esp.humidity_from(float("nan"), "") is None
    assert gadgets_esp.humidity_from(120, "") is None


# -- Cub summary block --------------------------------------------------------------------

def test_cub_hygrometers_block_is_pure_and_calm():
    rows = cub_svc.hygrometers_block([
        {"id": "AA", "name": "Fridge", "location": "Fridge", "temp_c": 3.9,
         "humidity": 51.0, "stale": False, "battery": 90},
        "junk",
        {"id": "BB"},
    ])
    assert rows[0] == {"id": "AA", "name": "Fridge", "location": "Fridge",
                       "temp_c": 3.9, "humidity": 51.0, "stale": False}
    assert rows[1] == {"id": "BB", "name": "", "location": "",
                       "temp_c": None, "humidity": None, "stale": True}
    assert cub_svc.hygrometers_block([]) == []


def test_cub_summary_includes_hygrometers(client):
    client.post("/gadgets/hygrometers", json={
        "id": "AA:BB:CC:DD:EE:01", "name": "Fridge", "protocol": "govee_hygro",
        "location": "Fridge"})
    client.post("/gadgets/readings", json=_hygro_push(4.1, 47.0))
    body = client.get("/cub/summary").json()
    assert body["hygrometers"] == [{
        "id": "AA:BB:CC:DD:EE:01", "name": "Fridge", "location": "Fridge",
        "temp_c": 4.1, "humidity": 47.0, "stale": False}]
