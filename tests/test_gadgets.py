"""Bluetooth kitchen thermometer support (FoodAssistant-6ivl).

Covers the pure BLE payload decoders (per protocol, against synthetic and
captured byte payloads), the reader daemon's config loader, the app-side
state file round trip, the fire-once-per-crossing alert semantics, and the
/gadgets endpoints the daemon and the Timers page talk to. No radio, bluez,
or bleak needed.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from foodassistant_gadgets import decoders  # noqa: E402
from foodassistant_gadgets import config as gd_config  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import gadgets, ha_events  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    gadgets.reset()
    ha_events.reset()
    yield
    gadgets.reset()
    ha_events.reset()


# -- Inkbird iBBQ decoder -----------------------------------------------------

def test_ibbq_realtime_decodes_tenths_per_probe():
    # Probe 1 at 30.0 C, probe 2 at 102.5 C.
    payload = (300).to_bytes(2, "little") + (1025).to_bytes(2, "little")
    assert decoders.decode_ibbq_realtime(payload) == [30.0, 102.5]


def test_ibbq_realtime_disconnected_probes_are_none():
    # 0xFFF6 and 0xFFFF are the empty-socket sentinels; position is kept.
    payload = bytes([0xF6, 0xFF]) + (215).to_bytes(2, "little") + bytes([0xFF, 0xFF])
    assert decoders.decode_ibbq_realtime(payload) == [None, 21.5, None]


def test_ibbq_realtime_ignores_trailing_odd_byte():
    payload = (250).to_bytes(2, "little") + b"\x01"
    assert decoders.decode_ibbq_realtime(payload) == [25.0]


def test_ibbq_battery_percentage():
    # Header 0x24, current 3275 mV, max 6550 mV -> 50%.
    payload = bytes([0x24]) + (3275).to_bytes(2, "little") + (6550).to_bytes(2, "little")
    assert decoders.decode_ibbq_battery(payload) == 50


def test_ibbq_battery_zero_max_uses_factory_default():
    payload = bytes([0x24]) + (6550).to_bytes(2, "little") + (0).to_bytes(2, "little")
    assert decoders.decode_ibbq_battery(payload) == 100


def test_ibbq_battery_rejects_other_headers():
    assert decoders.decode_ibbq_battery(bytes([0x04, 0xFF, 0x00, 0x00, 0x00])) is None
    assert decoders.decode_ibbq_battery(b"\x24\x01") is None


# -- ThermoPro TP25 decoder -----------------------------------------------------

# The captured example frame from the protocol docs: probe 4 at 32.5 C, the
# other sockets empty, device displaying Celsius.
TP25_EXAMPLE = bytes.fromhex("300f5a0c00ffffffffffff0325ffffffffc3")


def test_tp25_example_frame_decodes():
    frame = decoders.decode_tp25_frame(TP25_EXAMPLE)
    assert frame is not None
    assert frame["unit"] == "C" and frame["alarm"] is False
    assert frame["probes"] == [None, None, None, 32.5, None, None]


def test_tp25_bad_checksum_rejected():
    bad = TP25_EXAMPLE[:-1] + bytes([TP25_EXAMPLE[-1] ^ 0xFF])
    assert decoders.decode_tp25_frame(bad) is None


def test_tp25_wrong_type_and_short_frames_rejected():
    assert decoders.decode_tp25_frame(b"\x25" + TP25_EXAMPLE[1:]) is None
    assert decoders.decode_tp25_frame(b"\x30\x0f\x5a") is None
    assert decoders.decode_tp25_frame(b"") is None


def test_tp25_fahrenheit_frames_convert_to_celsius():
    # One probe reading 212.0 F (BCD 0x21 0x20) with the device in F mode.
    value = bytes([0x50, 0x0F, 0x00, 0x21, 0x20])
    body = bytes([0x30, len(value)]) + value
    frame = decoders.decode_tp25_frame(body + bytes([decoders.tp25_checksum(body)]))
    assert frame["unit"] == "F"
    assert frame["probes"] == [100.0]


def test_tp25_bcd_sign_and_sentinels():
    assert decoders.decode_tp25_bcd(0x03, 0x54) == pytest.approx(35.4)
    assert decoders.decode_tp25_bcd(0x83, 0x54) == pytest.approx(-35.4)  # sign bit
    assert decoders.decode_tp25_bcd(0x12, 0x35) == pytest.approx(123.5)
    assert decoders.decode_tp25_bcd(0xFF, 0xFF) is None
    assert decoders.decode_tp25_bcd(0xDD, 0xDD) is None
    assert decoders.decode_tp25_bcd(0xEE, 0xEE) is None


def test_tp25_handshake_and_commands_are_checksum_valid():
    for cmd in (decoders.TP25_HANDSHAKE, decoders.TP25_REQUEST_TEMPS):
        assert decoders.tp25_checksum(cmd[:-1]) == cmd[-1]
    # The handshake matches the captured known-good bytes from the vendor app.
    assert decoders.TP25_HANDSHAKE == bytes.fromhex("01098a7a13b73ed68b67c2a0")


# -- Combustion advertising decoder ----------------------------------------------

def _pack_combustion_temps(temps_c):
    packed = 0
    for i, t in enumerate(temps_c):
        raw = round((t + 20.0) / 0.05)
        packed |= (raw & 0x1FFF) << (13 * i)
    return packed.to_bytes(13, "little")


def _combustion_adv(temps_c, mode=0, color=0, probe_id_zero_based=0,
                    battery_low=False, product_type=1):
    mode_id = (mode & 0x03) | ((color & 0x07) << 2) | ((probe_id_zero_based & 0x07) << 5)
    return (bytes([product_type]) + (0x12345678).to_bytes(4, "little")
            + _pack_combustion_temps(temps_c)
            + bytes([mode_id, 0x01 if battery_low else 0x00]))


def test_combustion_normal_mode_decodes_all_eight_sensors():
    temps = [54.05, 54.5, 55.0, 60.0, 80.0, 120.0, 200.0, 250.0]
    out = decoders.decode_combustion_advertising(_combustion_adv(temps))
    assert out is not None
    assert out["serial"] == "12345678"
    assert out["instant_read"] is False and out["battery_low"] is False
    assert out["temps_c"] == pytest.approx(temps, abs=0.026)


def test_combustion_instant_read_keeps_only_the_first_sensor():
    temps = [63.0] + [0.0] * 7
    out = decoders.decode_combustion_advertising(
        _combustion_adv(temps, mode=1, probe_id_zero_based=3, battery_low=True))
    assert out["instant_read"] is True and out["probe_id"] == 4
    assert out["battery_low"] is True
    assert out["temps_c"][0] == pytest.approx(63.0, abs=0.026)
    assert out["temps_c"][1:] == [None] * 7


def test_combustion_rejects_non_probe_products_and_short_payloads():
    assert decoders.decode_combustion_advertising(
        _combustion_adv([25.0] * 8, product_type=2)) is None
    assert decoders.decode_combustion_advertising(b"\x01\x02") is None


# -- BlueDOT decoder ---------------------------------------------------------------

def _bluedot_frame(temp, alarm_temp, unit=0x00, status=0x00,
                   silenced=0, disabled=0, alarm_active=0):
    return (bytes([status]) + int(temp).to_bytes(4, "little", signed=True)
            + int(alarm_temp).to_bytes(4, "little", signed=True)
            + bytes([silenced, disabled, unit, 0x00])
            + b"\xAA\xBB\xCC\xDD\xEE\xFF" + bytes([alarm_active]))


def test_bluedot_celsius_frame():
    out = decoders.decode_bluedot(_bluedot_frame(74, 80))
    assert out["connected"] is True
    assert out["temp_c"] == 74.0 and out["alarm_temp_c"] == 80.0
    assert out["unit"] == "C" and out["alarm_active"] is False


def test_bluedot_fahrenheit_converts_to_celsius():
    out = decoders.decode_bluedot(_bluedot_frame(212, 165, unit=0x01, alarm_active=1))
    assert out["temp_c"] == pytest.approx(100.0)
    assert out["alarm_temp_c"] == pytest.approx(73.9, abs=0.1)
    assert out["unit"] == "F" and out["alarm_active"] is True


def test_bluedot_disconnected_probe_has_no_temp():
    out = decoders.decode_bluedot(_bluedot_frame(74, 80, status=0x03))
    assert out["connected"] is False and out["temp_c"] is None


def test_bluedot_wrong_length_rejected():
    assert decoders.decode_bluedot(b"\x00" * 19) is None
    assert decoders.decode_bluedot(b"\x00" * 21) is None


# -- ThermoPro TempSpike (TP96x) advertising decoder --------------------------------
# Ground truth: a live scan of Dan's TP960R (name "TP960R") at room temperature.
# bleak keys manufacturer data by company id; this device rolls the tip
# temperature's low byte through the company id, so a single advertisement
# carries two keyed frames. thermopro-ble reads the newest (last) key.
TEMPSPIKE_TP960R_MFR = {
    0x3300: bytes.fromhex("00540a3300"),
    0x3400: bytes.fromhex("00550a3600"),
}


def test_tempspike_decodes_captured_room_temp_frame():
    # Last key (0x3400): restored frame 00 34 00 55 0a 36 00 -> tip 0x34=52,
    # ambient 0x36=54, each minus the 30-degree offset.
    out = decoders.decode_tempspike_from_manufacturer(TEMPSPIKE_TP960R_MFR)
    assert out is not None
    assert out["tip_c"] == 22.0 and out["ambient_c"] == 24.0
    assert 15.0 <= out["tip_c"] <= 35.0 and 15.0 <= out["ambient_c"] <= 35.0
    assert out["probe_index"] == 0
    assert 80 <= out["battery"] <= 95   # 0x0a55 = 2645 raw -> ~89%


def test_tempspike_decodes_the_other_captured_frame():
    # The 0x3300 frame on its own: 00 33 00 54 0a 33 00 -> tip and ambient 21 C.
    out = decoders.decode_tempspike(bytes.fromhex("0033") + bytes.fromhex("00540a3300"))
    assert out["tip_c"] == 21.0 and out["ambient_c"] == 21.0


def test_tempspike_appended_mac_frame_and_bad_length():
    # A 13-byte frame (7 + a reversed MAC) decodes the same; the extra is ignored.
    frame = bytes.fromhex("0034") + bytes.fromhex("00550a3600") + b"\x11\x22\x33\x44\x55\x66"
    assert decoders.decode_tempspike(frame)["tip_c"] == 22.0
    assert decoders.decode_tempspike(b"\x00\x34\x00") is None    # too short
    assert decoders.decode_tempspike_from_manufacturer({}) is None


# -- Govee grill (H5182 + siblings) advertising decoder -----------------------------
# Ground truth: a live scan of Dan's Govee H5182 with both probes unplugged, so
# the current temperatures read the 0xFFFF (-1) empty-socket sentinel while the
# stored alarm/target temperatures remain.
GOVEE_H5182_VALUE = bytes.fromhex("8201000101e40106ffff1d4c06ffff1cdc")


def test_govee_grill_h5182_captured_unplugged_probes_and_targets():
    out = decoders.decode_govee_grill(GOVEE_H5182_VALUE)
    assert out is not None
    assert out["probes"] == [None, None]          # both unplugged (0xffff)
    assert out["targets"] == [75.0, 73.88]        # 0x1d4c and 0x1cdc, /100


def test_govee_grill_reads_plugged_probe_temps():
    # A synthetic H5182 frame with both probes reporting (2530 -> 25.30 C).
    import struct as _s
    value = (bytes.fromhex("8201000101000106")   # 8 header bytes, temps at 8
             + _s.pack(">hhbhh", 2530, 7500, 0x06, 1899, 7000))
    out = decoders.decode_govee_grill(value)
    assert out["probes"] == [25.3, 18.99]
    assert out["targets"] == [75.0, 70.0]


def test_govee_grill_rejects_unknown_length():
    assert decoders.decode_govee_grill(b"\x82\x01\x00") is None


# -- Room-sensor filter: keep hygrometers out of the thermometer list ---------------

def test_hygrometer_filter_excludes_govee_gvh5075_room_sensors():
    # The six captured GVH5075 hygrometers (name GVH5075_xxxx, mfr 0xec88) are
    # room sensors, not cooking probes, and must be filtered from discovery.
    for i in range(6):
        name = f"GVH5075_{i:04X}"
        assert decoders.is_room_sensor(name, {0xEC88: b"\x88\xec\x00\x01"}) is True
    # By name alone (all Govee GVH50xx are ambient sensors) and by the
    # hygrometer company id alone.
    assert decoders.is_room_sensor("GVH5075_1A2B") is True
    assert decoders.is_room_sensor(None, {decoders.GOVEE_HYGROMETER_MANUFACTURER_ID: b""}) is True


def test_hygrometer_filter_passes_the_govee_h5182_grill():
    # The H5182 grill (GVH518x, its own company id and payload) is a cooking
    # probe and must reach the discovery list.
    mfr = {0x4831: GOVEE_H5182_VALUE, 0x004C: b"\x02\x15" + b"\x00" * 21}
    assert decoders.is_room_sensor("GVH5182_9ABC", mfr) is False
    assert decoders.is_room_sensor(None, mfr) is False
    assert decoders.identify(None, mfr) == "govee_grill"


# -- Seen-but-unsupported name heuristic --------------------------------------------

def test_looks_like_probe_flags_unsupported_but_probe_shaped_names():
    # iDevices Kitchen Thermometer advertises the bare name "KT"; iGrill and
    # Meater are probe-shaped names we have no decoder for.
    assert decoders.looks_like_probe("KT") is True
    assert decoders.looks_like_probe("iGrill mini") is True
    assert decoders.looks_like_probe("Meater") is True
    assert decoders.looks_like_probe("TP-Unknown9") is True
    # Not probe-shaped: headphones, empty, and a plain room sensor name.
    assert decoders.looks_like_probe("Some Headphones") is False
    assert decoders.looks_like_probe("") is False
    # KT is unsupported (no decoder): identify returns None so the daemon
    # routes it through looks_like_probe to a seen-but-unsupported row.
    assert decoders.identify("KT") is None


# -- Auto-detection ------------------------------------------------------------------

def test_identify_by_manufacturer_and_name():
    assert decoders.identify("anything", {0x09C7: b"\x01"}) == "combustion"
    assert decoders.identify("iBBQ-4") == "inkbird"
    assert decoders.identify("IBT-6XS") == "inkbird"
    assert decoders.identify("TP25W (52C4)") == "thermopro"
    assert decoders.identify("TP960R") == "tempspike"
    assert decoders.identify("TempSpike") == "tempspike"
    assert decoders.identify("GVH5182_9ABC") == "govee_grill"
    assert decoders.identify("BlueDOT") == "bluedot"
    assert decoders.identify("Some Headphones") is None
    assert decoders.identify(None) is None
    assert decoders.identify("", None, [decoders.IBBQ_SERVICE_UUID]) == "inkbird"
    # A Govee hygrometer is never a thermometer match.
    assert decoders.identify("GVH5075_1234", {0xEC88: b""}) is None


# -- Daemon config loader --------------------------------------------------------------

def test_gadget_config_defaults_and_toml(tmp_path, monkeypatch):
    monkeypatch.delenv(gd_config.ENV_BASE_URL, raising=False)
    monkeypatch.delenv(gd_config.ENV_API_KEY, raising=False)
    cfg = gd_config.load(tmp_path / "missing.toml")
    assert cfg.base_url == "http://127.0.0.1:9284" and cfg.push_seconds == 5

    f = tmp_path / "gadgets.toml"
    f.write_text('base_url = "http://box:9284/"\npush_seconds = 1\n'
                 '[[devices]]\nid = "aa:bb:cc:dd:ee:ff"\nprotocol = "inkbird"\n')
    cfg = gd_config.load(f)
    assert cfg.base_url == "http://box:9284"   # trailing slash trimmed
    assert cfg.push_seconds == 2               # clamped to the floor
    assert cfg.devices[0]["id"] == "aa:bb:cc:dd:ee:ff"

    monkeypatch.setenv(gd_config.ENV_API_KEY, "sekrit")
    assert gd_config.load(f).api_key == "sekrit"


# -- App-side state round trip -----------------------------------------------------------

def _push(temp_c=54.0, dev_id="AA:BB:CC:DD:EE:FF", probe=1, **kw):
    return {"devices": [{
        "id": dev_id, "name": "Grill", "protocol": "inkbird",
        "probes": [{"index": probe, "temp_c": temp_c}],
        "battery": kw.get("battery", 80), "rssi": kw.get("rssi", -60),
    }], "discovered": kw.get("discovered", [])}


def test_state_file_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "gadgets_enabled", True, raising=False)
    monkeypatch.setattr(settings, "gadget_devices",
                        [{"id": "AA:BB:CC:DD:EE:FF", "name": "Grill",
                          "protocol": "inkbird", "targets": {}}], raising=False)
    out = gadgets.ingest(_push(54.0))
    assert out["ok"] is True and out["stored"] == 1
    # The state survives a "different worker": wipe the in-process cache and
    # re-read from the file.
    gadgets._mtime = None
    gadgets._state = {"devices": {}, "discovered": {}, "alerts": {}}
    state = gadgets.get_state()
    assert state["enabled"] is True
    dev = state["devices"][0]
    assert dev["id"] == "AA:BB:CC:DD:EE:FF" and dev["battery"] == 80
    assert dev["probes"][0]["temp_c"] == 54.0 and dev["stale"] is False
    raw = json.loads((tmp_path / "gadgets.json").read_text())
    assert "AA:BB:CC:DD:EE:FF" in raw["devices"]


def test_discovered_devices_listed_until_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "gadgets_enabled", False, raising=False)
    monkeypatch.setattr(settings, "gadget_devices", [], raising=False)
    gadgets.ingest({"devices": [], "discovered": [
        {"id": "11:22:33:44:55:66", "name": "iBBQ", "protocol": "inkbird", "rssi": -70},
    ]})
    state = gadgets.get_state()
    assert state["enabled"] is False
    assert state["discovered"][0]["id"] == "11:22:33:44:55:66"
    # Once configured it leaves the add list.
    monkeypatch.setattr(settings, "gadget_devices",
                        [{"id": "11:22:33:44:55:66", "protocol": "inkbird"}], raising=False)
    assert gadgets.get_state()["discovered"] == []


def test_discovered_seen_but_unsupported_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "gadgets_enabled", False, raising=False)
    monkeypatch.setattr(settings, "gadget_devices", [], raising=False)
    gadgets.ingest({"devices": [], "discovered": [
        {"id": "D4:81:CA:10:35:0B", "name": "KT", "protocol": "", "supported": False},
        {"id": "11:22:33:44:55:66", "name": "iBBQ", "protocol": "inkbird"},
    ]})
    seen = {d["id"]: d for d in gadgets.get_state()["discovered"]}
    # The iDevices "KT" surfaces as seen-but-unsupported; the iBBQ is supported.
    assert seen["D4:81:CA:10:35:0B"]["supported"] is False
    assert seen["11:22:33:44:55:66"]["supported"] is True


def test_default_probe_role_maps_tempspike_leads():
    assert gadgets.default_probe_role("tempspike", 1) == "internal"
    assert gadgets.default_probe_role("tempspike", 2) == "ambient"
    # Every other protocol leaves probes unlabeled.
    assert gadgets.default_probe_role("govee_grill", 1) == ""
    assert gadgets.default_probe_role("inkbird", 2) == ""
    assert gadgets.role_label("ambient") == "Ambient"
    assert gadgets.role_label("") == ""


def test_normalize_reading_keeps_role_and_device_target():
    r = gadgets.normalize_reading(
        {"id": "AA:BB", "protocol": "govee_grill", "probes": [
            {"index": 1, "temp_c": 20.0, "device_target_c": 75.0},
            {"index": 2, "temp_c": None, "role": "ambient"},
            {"index": 3, "temp_c": 30.0, "role": "bogus", "device_target_c": 9999.0},
        ]}, 1000.0)
    assert r["probes"][0]["device_target_c"] == 75.0
    assert r["probes"][1]["role"] == "ambient"
    # A role outside the whitelist and an out-of-range target are dropped.
    assert "role" not in r["probes"][2]
    assert "device_target_c" not in r["probes"][2]


def test_get_state_resolves_probe_role_and_setpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "gadgets_enabled", True, raising=False)
    monkeypatch.setattr(settings, "gadget_devices",
                        [{"id": "AA:BB:CC:DD:EE:FF", "name": "Smoker",
                          "protocol": "tempspike", "targets": {}}], raising=False)
    gadgets.ingest({"devices": [{
        "id": "AA:BB:CC:DD:EE:FF", "protocol": "tempspike", "probes": [
            {"index": 1, "temp_c": 60.0, "role": "internal"},
            {"index": 2, "temp_c": 110.0, "role": "ambient",
             "device_target_c": 120.0},
        ], "rssi": -50}]})
    probes = gadgets.get_state()["devices"][0]["probes"]
    # Reader-tagged roles surface with labels and read as auto (no override).
    assert probes[0]["role"] == "internal" and probes[0]["role_label"] == "Internal"
    assert probes[0]["role_source"] == "auto"
    # The device's own broadcast setpoint shows when the user set no target.
    assert probes[1]["device_target_c"] == 120.0 and probes[1]["target_c"] is None


def test_get_state_probe_role_override_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "gadgets_enabled", True, raising=False)
    monkeypatch.setattr(settings, "gadget_devices",
                        [{"id": "AA:BB:CC:DD:EE:FF", "name": "Smoker",
                          "protocol": "tempspike",
                          "roles": {"1": "ambient"}, "targets": {}}], raising=False)
    gadgets.ingest({"devices": [{
        "id": "AA:BB:CC:DD:EE:FF", "protocol": "tempspike", "probes": [
            {"index": 1, "temp_c": 60.0, "role": "internal"},
        ], "rssi": -50}]})
    probe = gadgets.get_state()["devices"][0]["probes"][0]
    # The user override beats both the reader tag and the protocol default.
    assert probe["role"] == "ambient" and probe["role_source"] == "you"


def test_bluetooth_off_state_threads_to_get_state(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "gadgets_enabled", True, raising=False)
    monkeypatch.setattr(settings, "gadget_devices", [], raising=False)
    # The reader reports its radio powered off, with nothing else to send.
    gadgets.ingest({"devices": [], "discovered": [],
                    "bluetooth": {"available": False, "detail": "rfkill blocked"}})
    # Survives a "different worker" re-read from the file.
    gadgets._mtime = None
    gadgets._state = {"devices": {}, "discovered": {}, "alerts": {},
                      "reader_seen": 0.0, "bluetooth": {"available": True, "detail": ""}}
    assert gadgets.get_state()["bluetooth_available"] is False
    # Radio back on: the flag clears.
    gadgets.ingest({"devices": [], "bluetooth": {"available": True, "detail": ""}})
    assert gadgets.get_state()["bluetooth_available"] is True
    # The Home Assistant poller (mark_reader=False) never touches adapter state.
    gadgets.ingest({"devices": [], "bluetooth": {"available": False}}, mark_reader=False)
    assert gadgets.get_state()["bluetooth_available"] is True


def test_normalize_reading_rejects_junk():
    assert gadgets.normalize_reading({"id": "", "probes": []}, 0) is None
    assert gadgets.normalize_reading("nope", 0) is None
    assert gadgets.normalize_reading({"id": "x", "probes": [{"index": "bad"}]}, 0) is None
    r = gadgets.normalize_reading(
        {"id": "aa", "probes": [{"index": 1, "temp_c": 9999}], "battery": 400}, 5.0)
    assert r["probes"][0]["temp_c"] is None       # out-of-range clamps to no reading
    assert r["battery"] == 100 and r["ts"] == 5.0


# -- Alert semantics: once per crossing ------------------------------------------------------

def test_alert_fires_once_per_crossing_and_rearms():
    targets = {"D:1": {"temp_c": 74.0, "direction": "above"}}
    state: dict = {}
    # Below target: armed, nothing fires.
    state, fired = gadgets.evaluate_alerts(targets, {"D:1": 60.0}, state, now=1000)
    assert fired == []
    # Crossing fires once.
    state, fired = gadgets.evaluate_alerts(targets, {"D:1": 74.5}, state, now=1010)
    assert len(fired) == 1 and fired[0]["key"] == "D:1"
    # Staying above stays quiet, reading after reading.
    for t in (75.0, 80.0, 90.0):
        state, fired = gadgets.evaluate_alerts(targets, {"D:1": t}, state, now=1020)
        assert fired == []
    # Dipping just under the target (within hysteresis) does not re-arm.
    state, fired = gadgets.evaluate_alerts(targets, {"D:1": 73.8}, state, now=1030)
    state, fired = gadgets.evaluate_alerts(targets, {"D:1": 74.2}, state, now=1040)
    assert fired == []
    # A real drop re-arms, and the next crossing (past the cooldown) fires again.
    state, fired = gadgets.evaluate_alerts(targets, {"D:1": 60.0}, state, now=1050)
    assert fired == []
    state, fired = gadgets.evaluate_alerts(targets, {"D:1": 75.0}, state, now=2000)
    assert len(fired) == 1


def test_alert_cooldown_suppresses_rapid_recrossings():
    targets = {"D:1": {"temp_c": 74.0, "direction": "above"}}
    state, fired = gadgets.evaluate_alerts(targets, {"D:1": 75.0}, {}, now=1000)
    assert len(fired) == 1
    # Re-arm and re-cross within the cooldown: quiet.
    state, fired = gadgets.evaluate_alerts(targets, {"D:1": 60.0}, state, now=1010)
    state, fired = gadgets.evaluate_alerts(targets, {"D:1": 76.0}, state, now=1020)
    assert fired == []
    # The same re-crossing after the cooldown fires.
    state, fired = gadgets.evaluate_alerts(targets, {"D:1": 60.0}, state, now=1030)
    state, fired = gadgets.evaluate_alerts(targets, {"D:1": 76.0}, state, now=1000 + 400)
    assert len(fired) == 1


def test_alert_below_direction_and_missing_reading():
    targets = {"D:1": {"temp_c": 4.0, "direction": "below"}}
    state, fired = gadgets.evaluate_alerts(targets, {"D:1": 10.0}, {}, now=0)
    assert fired == []
    state, fired = gadgets.evaluate_alerts(targets, {"D:1": 3.5}, state, now=10)
    assert len(fired) == 1 and fired[0]["direction"] == "below"
    # A dropout keeps the reached state: no re-fire when the reading returns.
    state, fired = gadgets.evaluate_alerts(targets, {}, state, now=20)
    state, fired = gadgets.evaluate_alerts(targets, {"D:1": 3.5}, state, now=30)
    assert fired == []


def test_alert_already_past_target_on_first_sight_fires():
    # Setting a target below the current temperature alerts right away.
    targets = {"D:1": {"temp_c": 50.0, "direction": "above"}}
    _, fired = gadgets.evaluate_alerts(targets, {"D:1": 90.0}, {}, now=0)
    assert len(fired) == 1


def test_ingest_fires_ha_events_toast(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "gadgets_enabled", True, raising=False)
    monkeypatch.setattr(settings, "streamdeck_weather_units", "f", raising=False)
    monkeypatch.setattr(settings, "gadget_devices",
                        [{"id": "AA:BB:CC:DD:EE:FF", "name": "Brisket",
                          "protocol": "inkbird",
                          "targets": {"1": {"temp_c": 74.0, "direction": "above"}}}],
                        raising=False)
    out = gadgets.ingest(_push(temp_c=75.0))
    assert out["alerts"] == 1
    ev = ha_events.poll(0)["events"][-1]
    assert ev["type"] == "warning" and ev["title"] == "Brisket"
    assert "165°F" in ev["message"]
    assert ev["key"] == "gadget:AA:BB:CC:DD:EE:FF:1"
    # Same reading again: still above target, no second toast.
    before = ha_events.poll(0)["last_id"]
    gadgets.ingest(_push(temp_c=76.0))
    assert ha_events.poll(0)["last_id"] == before


def test_format_temp_units():
    assert gadgets.format_temp(74.0, "f") == "165°F"
    assert gadgets.format_temp(74.0, "c") == "74°C"


# -- Endpoints --------------------------------------------------------------------------------

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
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_config_endpoint_shape(client):
    data = client.get("/gadgets/config").json()
    assert data["enabled"] is False and data["devices"] == []
    # Hygrometers ride the same reader config (FoodAssistant-q97i).
    assert data["hygrometers_enabled"] is False and data["hygrometers"] == []
    # Buttons too (FoodAssistant-771d).
    assert data["buttons_enabled"] is False and data["buttons"] == []
    # The BLE status broadcast flag (FoodAssistant-yl6u): off by default,
    # with the install id along for the packet's sender tag.
    assert data["cub_ble_advertise"] is False
    assert data["device_id"]


def test_add_device_enables_and_round_trips(client):
    r = client.post("/gadgets/devices", json={
        "id": "aa:bb:cc:dd:ee:ff", "name": "Grill", "protocol": "inkbird"}).json()
    assert r["ok"] is True
    data = client.get("/gadgets/config").json()
    assert data["enabled"] is True
    assert data["devices"][0]["id"] == "AA:BB:CC:DD:EE:FF"
    assert data["devices"][0]["protocol"] == "inkbird"
    # Re-adding updates rather than duplicating.
    client.post("/gadgets/devices", json={"id": "AA:BB:CC:DD:EE:FF", "name": "Smoker"})
    devices = client.get("/gadgets/config").json()["devices"]
    assert len(devices) == 1 and devices[0]["name"] == "Smoker"


def test_add_device_requires_id(client):
    assert client.post("/gadgets/devices", json={"id": "  "}).json()["ok"] is False


def test_target_set_clear_and_unknown_device(client):
    client.post("/gadgets/devices", json={"id": "AA:BB:CC:DD:EE:FF", "protocol": "inkbird"})
    r = client.post("/gadgets/target", json={
        "device_id": "aa:bb:cc:dd:ee:ff", "probe": 1, "temp_c": 74.0,
        "direction": "above"}).json()
    assert r["ok"] is True
    dev = client.get("/gadgets/config").json()["devices"][0]
    assert dev["targets"]["1"] == {"temp_c": 74.0, "direction": "above"}
    r = client.post("/gadgets/target", json={
        "device_id": "AA:BB:CC:DD:EE:FF", "probe": 1, "temp_c": None}).json()
    assert r["ok"] is True
    assert client.get("/gadgets/config").json()["devices"][0]["targets"] == {}
    r = client.post("/gadgets/target", json={
        "device_id": "no:such:dev", "probe": 1, "temp_c": 50.0}).json()
    assert r["ok"] is False


def test_remove_device(client):
    client.post("/gadgets/devices", json={"id": "AA:BB:CC:DD:EE:FF"})
    assert client.delete("/gadgets/devices/aa:bb:cc:dd:ee:ff").json()["ok"] is True
    assert client.get("/gadgets/config").json()["devices"] == []


def test_readings_and_state_endpoints(client):
    client.post("/gadgets/devices", json={"id": "AA:BB:CC:DD:EE:FF",
                                          "name": "Grill", "protocol": "inkbird"})
    r = client.post("/gadgets/readings", json=_push(65.0)).json()
    assert r["ok"] is True and r["stored"] == 1
    state = client.get("/gadgets/state").json()
    assert state["enabled"] is True and state["unit"] in ("f", "c")
    dev = state["devices"][0]
    assert dev["name"] == "Grill" and dev["probes"][0]["temp_c"] == 65.0
    assert dev["probes"][0]["target_c"] is None
    # Discovered devices ride the same push and show up for the UI.
    client.post("/gadgets/readings", json={"devices": [], "discovered": [
        {"id": "11:22:33:44:55:66", "name": "TP25", "protocol": "thermopro", "rssi": -55}]})
    state = client.get("/gadgets/state").json()
    assert state["discovered"][0]["name"] == "TP25"


def test_state_merges_targets(client):
    client.post("/gadgets/devices", json={"id": "AA:BB:CC:DD:EE:FF", "protocol": "inkbird"})
    client.post("/gadgets/target", json={"device_id": "AA:BB:CC:DD:EE:FF",
                                         "probe": 1, "temp_c": 74.0, "direction": "above"})
    client.post("/gadgets/readings", json=_push(60.0))
    probe = client.get("/gadgets/state").json()["devices"][0]["probes"][0]
    assert probe["target_c"] == 74.0 and probe["direction"] == "above"


def test_timers_page_has_probes_section(client):
    html = client.get("/ui/timers").text
    assert "probesSection" in html and "gadgets/state" in html
