"""Cub BLE advertisement relay (FoodAssistant-nn3u).

A Cub with its radio on forwards the raw advertisements of the kitchen
sensors near it to POST /cub/ble-adv, and the server decodes them with the
same decoders the reader daemon uses, so a Docker server with no Bluetooth
radio still sees the fridge hygrometer, the door sensor, and the shelf
button. Covers the allowlist derivation, the raw advertisement parser, the
endpoint's validation matrix, decode-through into the gadgets ingest with
the "via Cub" tag, the default-off gate, and the per-Cub rate limit. No
radio and no network needed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from foodassistant_gadgets import decoders  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services import cub as cub_svc  # noqa: E402
from app.services import gadgets, gadgets_buttons  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    gadgets.reset()
    cub_svc.reset_relay_state()
    old = (settings.cub_ble_relay, settings.hygrometer_devices,
           settings.contact_devices, settings.button_devices,
           settings.gadget_devices)
    settings.cub_ble_relay = True
    settings.hygrometer_devices = []
    settings.contact_devices = []
    settings.button_devices = []
    settings.gadget_devices = []
    yield
    (settings.cub_ble_relay, settings.hygrometer_devices,
     settings.contact_devices, settings.button_devices,
     settings.gadget_devices) = old
    gadgets.reset()
    cub_svc.reset_relay_state()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "api_key", "", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", [], raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr("app.hardware.is_raspberry_pi", lambda: False)
    with TestClient(app) as c:
        yield c


def _post(client, packets, cub="cub-1", name="Kitchen"):
    return client.post("/cub/ble-adv", json={"packets": packets},
                       headers={"X-API-Key": settings.api_key or "",
                                "X-Cub-Id": cub, "X-Cub-Name": name})


# -- Advertisement vectors -----------------------------------------------------
#
# Built by wrapping the decoders' own proven payloads in real AD structures
# (length, type, data), which is exactly what a radio hands the Cub.

def _ad(ad_type: int, data: bytes) -> bytes:
    return bytes([len(data) + 1, ad_type]) + data


def _manufacturer_ad(company: int, value: bytes) -> bytes:
    return _ad(0xFF, company.to_bytes(2, "little") + value)


def _service_data_ad(uuid16: int, value: bytes) -> bytes:
    return _ad(0x16, uuid16.to_bytes(2, "little") + value)


def _name_ad(name: str) -> bytes:
    return _ad(0x09, name.encode())


# The documented Govee H5075 reading from tests/test_hygrometers.py: 20.51 C,
# 14.6 %, battery 100, on Govee's hygrometer company id.
GOVEE_VALUE = bytes([0x00, 0x03, 0x21, 0x5A, 0x64, 0x00])
GOVEE_ADV = (_ad(0x01, bytes([0x06]))
             + _name_ad("GVH5075_1234")
             + _manufacturer_ad(decoders.GOVEE_HYGROMETER_MANUFACTURER_ID,
                                GOVEE_VALUE))

# A genuine BTHome v2 Shelly BLU Door/Window frame (tests/test_gadget_alarms.py):
# battery 100 %, illuminance, window open, rotation.
BTHOME_OPEN = bytes([0x44, 0x01, 0x64, 0x05, 0x10, 0x27, 0x00,
                     0x2D, 0x01, 0x3F, 0x00, 0x00])
BTHOME_ADV = _service_data_ad(0xFCD2, BTHOME_OPEN)

MAC = "AA:BB:CC:DD:EE:FF"


# -- Allowlist derivation ------------------------------------------------------

def test_allowlist_is_derived_from_what_the_decoders_match():
    block = cub_svc.ble_relay_block(settings)
    # Every company id and service UUID comes from a decoder constant, so the
    # allowlist cannot drift away from what the server can actually read.
    assert decoders.COMBUSTION_MANUFACTURER_ID in block["company_ids"]
    assert decoders.GOVEE_HYGROMETER_MANUFACTURER_ID in block["company_ids"]
    assert 0xFCD2 in block["service_uuids"]   # BTHome v2
    assert 0xFE95 in block["service_uuids"]   # Xiaomi MiBeacon
    assert 0x181A in block["service_uuids"]   # ATC / pvvx
    assert 0xFD3D in block["service_uuids"]   # SwitchBot
    assert 0x0D00 in block["service_uuids"]   # SwitchBot, older firmware
    # The name-matched family: their company id carries temperature bytes, so
    # only the name is stable enough to filter on.
    assert "ibs-th" in block["names"] and "tempspike" in block["names"]
    assert "gvh50" in block["names"]


def test_allowlist_excludes_the_connect_only_brands():
    # iBBQ (0xFFF0) and TP25 need a GATT connection: their advertisement
    # carries no reading, so relaying it would promise what a radio-less
    # server can never deliver. docs/design/bandit-cub.md says so plainly.
    block = cub_svc.ble_relay_block(settings)
    assert 0xFFF0 not in block["service_uuids"]
    assert all(not n.startswith("tp25") for n in block["names"])


def test_allowlist_uuids_match_the_decoders_own_uuid_strings():
    block = cub_svc.ble_relay_block(settings)
    for full in (decoders.BTHOME_SERVICE_UUID, decoders.XIAOMI_SERVICE_UUID,
                 decoders.ATC_SERVICE_UUID):
        assert int(full[4:8], 16) in block["service_uuids"]


# -- Summary gating ------------------------------------------------------------

def test_summary_omits_the_relay_block_by_default(client):
    settings.cub_ble_relay = False
    body = client.get("/cub/summary",
                      headers={"X-API-Key": settings.api_key or ""}).json()
    assert "ble_relay" not in body


def test_summary_carries_the_relay_block_when_on(client):
    body = client.get("/cub/summary",
                      headers={"X-API-Key": settings.api_key or ""}).json()
    assert body["ble_relay"]["enabled"] is True
    assert body["ble_relay"]["max_packets"] == cub_svc.BLE_RELAY_BATCH_MAX
    assert body["ble_relay"]["interval_ms"] == cub_svc.BLE_RELAY_BATCH_MS


# -- The raw advertisement parser ----------------------------------------------

def test_parse_advertisement_reads_name_manufacturer_and_service_data():
    adv = cub_svc.parse_advertisement(GOVEE_ADV)
    assert adv["name"] == "GVH5075_1234"
    assert adv["manufacturer_data"] == {
        decoders.GOVEE_HYGROMETER_MANUFACTURER_ID: GOVEE_VALUE}
    adv = cub_svc.parse_advertisement(BTHOME_ADV)
    assert adv["service_data"] == {
        "0000fcd2-0000-1000-8000-00805f9b34fb": BTHOME_OPEN}


def test_parse_advertisement_survives_garbage():
    # A truncated structure, a zero length, and pure noise are all things a
    # real radio sees; none of them may raise.
    for raw in (b"\x20\xff\x01", b"\x00\x00\x00", b"\xff" * 40, b""):
        assert isinstance(cub_svc.parse_advertisement(raw), dict)


def test_parse_advertisement_keeps_manufacturer_key_order():
    # The TempSpike and the Inkbird IBS-TH roll temperature bytes through the
    # company id and their decoders read the LAST key, so order is load-bearing.
    raw = _manufacturer_ad(0x0001, b"\x01") + _manufacturer_ad(0x0002, b"\x02")
    assert list(cub_svc.parse_advertisement(raw)["manufacturer_data"]) == [1, 2]


# -- Decode-through ------------------------------------------------------------

def test_configured_hygrometer_decodes_through_to_a_real_reading(client):
    settings.hygrometer_devices = [{"id": MAC, "name": "Fridge",
                                    "protocol": "govee_hygro"}]
    r = _post(client, [{"mac": MAC, "rssi": -60, "adv": GOVEE_ADV.hex()}])
    assert r.status_code == 200 and r.json()["readings"] == 1

    live = gadgets.get_state()["hygrometers"]
    entry = next(e for e in live if e["id"] == MAC)
    assert entry["temp_c"] == 20.51 and entry["humidity"] == 14.6
    # The card must be able to say where the reading was heard.
    assert entry["via"] == "Cub Kitchen"


def test_configured_contact_decodes_a_genuine_bthome_frame(client):
    settings.contact_devices = [{"id": MAC, "name": "Freezer door",
                                 "protocol": "bthome_contact"}]
    r = _post(client, [{"mac": MAC, "rssi": -70, "adv": BTHOME_ADV.hex()}])
    assert r.status_code == 200 and r.json()["readings"] == 1

    entry = next(e for e in gadgets.get_state()["contacts"] if e["id"] == MAC)
    assert entry["open"] is True and entry["battery"] == 100
    assert entry["via"] == "Cub Kitchen"


def test_an_unknown_sensor_arrives_as_discovered_not_as_a_reading(client):
    r = _post(client, [{"mac": MAC, "rssi": -60, "adv": GOVEE_ADV.hex()}])
    assert r.status_code == 200 and r.json()["matched"] == 1
    state = gadgets.get_state()
    assert not [e for e in state["hygrometers"] if e["id"] == MAC]
    seen = next(e for e in state["hygro_discovered"] if e["id"] == MAC)
    assert seen["protocol"] == "govee_hygro" and seen["via"] == "Cub Kitchen"


def test_a_relayed_push_never_claims_a_local_bluetooth_reader(client):
    settings.hygrometer_devices = [{"id": MAC, "name": "Fridge",
                                    "protocol": "govee_hygro"}]
    _post(client, [{"mac": MAC, "rssi": -60, "adv": GOVEE_ADV.hex()}])
    # The Cub is a radio somewhere else, not this server's own reader, so the
    # Settings pane must not start claiming a local Bluetooth reader.
    state = gadgets.get_state()
    assert state["reader_age_seconds"] is None
    # It IS a relay, and the pane can say so.
    assert state["relay_source"] == "Cub Kitchen"


def test_unrelated_advertisements_are_ignored_quietly(client):
    # An Apple iBeacon-ish frame nothing decodes: no reading, no discovery.
    adv = _manufacturer_ad(0x004C, b"\x02\x15" + b"\x00" * 20)
    r = _post(client, [{"mac": MAC, "rssi": -60, "adv": adv.hex()}])
    assert r.status_code == 200
    assert r.json()["matched"] == 0 and r.json()["readings"] == 0


# -- Endpoint validation matrix ------------------------------------------------

def test_relay_is_off_by_default(client):
    settings.cub_ble_relay = False
    r = _post(client, [{"mac": MAC, "rssi": -60, "adv": GOVEE_ADV.hex()}])
    assert r.status_code == 403 and r.json()["reason"] == "relay_off"


def test_endpoint_needs_a_key(client, monkeypatch):
    # /cub is deliberately in no auth bypass list: a Cub authenticates with the
    # same X-API-Key every headless client uses, and the relay is no exception.
    monkeypatch.setattr(settings, "api_key", "s3cret", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", "pw", raising=False)
    r = client.post("/cub/ble-adv", json={"packets": []},
                    headers={"X-Cub-Id": "cub-1"}, follow_redirects=False)
    assert r.status_code != 200
    r = client.post("/cub/ble-adv", json={"packets": []},
                    headers={"X-Cub-Id": "cub-1", "X-API-Key": "s3cret"},
                    follow_redirects=False)
    assert r.status_code == 200


@pytest.mark.parametrize("body", [
    {"packets": "nope"},
    {"packets": {"mac": MAC}},
    {"nothing": 1},
    [],
    "garbage",
])
def test_a_malformed_body_is_a_calm_400_never_a_500(client, body):
    r = client.post("/cub/ble-adv", json=body,
                    headers={"X-API-Key": settings.api_key or "",
                             "X-Cub-Id": "cub-1"})
    assert r.status_code == 400


def test_too_many_packets_is_refused(client):
    packets = [{"mac": MAC, "rssi": -60, "adv": GOVEE_ADV.hex()}
               for _ in range(cub_svc.BLE_RELAY_MAX_PACKETS + 1)]
    r = _post(client, packets)
    assert r.status_code == 400 and r.json()["reason"] == "too_many_packets"


@pytest.mark.parametrize("packet", [
    {"mac": "not-a-mac", "rssi": -60, "adv": "0201060303"},
    {"mac": MAC, "rssi": -60, "adv": "zzzz"},          # not hex
    {"mac": MAC, "rssi": -60, "adv": "abc"},           # odd length
    {"mac": MAC, "rssi": -60, "adv": "ab" * 63},       # longer than a real advert
    {"mac": MAC, "rssi": -60},                         # no advertisement at all
    {"adv": "020106"},                                 # no MAC
    {"mac": MAC, "rssi": "loud", "adv": "020106"},     # RSSI of the wrong type
    "not-even-a-dict",
])
def test_a_bad_packet_is_a_counted_drop_not_an_error(client, packet):
    r = _post(client, [packet])
    assert r.status_code == 200
    body = r.json()
    # A wrong-typed RSSI is survivable (the packet still decodes); the rest
    # are drops. Either way the batch is answered calmly.
    assert body["dropped"] + body["accepted"] == 1


def test_unknown_packet_fields_are_ignored(client):
    settings.hygrometer_devices = [{"id": MAC, "name": "Fridge",
                                    "protocol": "govee_hygro"}]
    r = _post(client, [{"mac": MAC, "rssi": -60, "adv": GOVEE_ADV.hex(),
                        "future_field": {"anything": 1}}])
    assert r.status_code == 200 and r.json()["readings"] == 1


def test_a_good_packet_still_lands_when_a_bad_one_shares_the_batch(client):
    settings.hygrometer_devices = [{"id": MAC, "name": "Fridge",
                                    "protocol": "govee_hygro"}]
    r = _post(client, [{"mac": "bogus", "adv": "zz"},
                       {"mac": MAC, "rssi": -60, "adv": GOVEE_ADV.hex()}])
    assert r.status_code == 200
    assert r.json()["dropped"] == 1 and r.json()["readings"] == 1


# -- Rate limiting -------------------------------------------------------------

def test_a_cub_that_floods_gets_rate_limited(client):
    codes = set()
    for _ in range(int(cub_svc.BLE_RELAY_BURST) + 6):
        codes.add(_post(client, []).status_code)
    assert 429 in codes


def test_the_rate_limit_is_per_cub(client):
    for _ in range(int(cub_svc.BLE_RELAY_BURST) + 6):
        _post(client, [], cub="cub-noisy")
    # A second Cub still has its own full bucket.
    assert _post(client, [], cub="cub-quiet").status_code == 200


def test_the_bucket_refills_over_time():
    now = 1000.0
    for _ in range(int(cub_svc.BLE_RELAY_BURST)):
        assert cub_svc.relay_rate_ok("cub-1", now) is True
    assert cub_svc.relay_rate_ok("cub-1", now) is False
    # A second later there is room again, at the configured refill rate.
    assert cub_svc.relay_rate_ok("cub-1", now + 1.0) is True


# -- Buttons -------------------------------------------------------------------

# A BTHome v2 button frame from tests/test_gadgets_buttons.py: packet id,
# battery 100 %, one button object carrying a single press.
BTHOME_SINGLE = bytes([0x40, 0x00, 0x05, 0x01, 0x64, 0x3A, 0x01])
BUTTON_ADV = _service_data_ad(0xFCD2, BTHOME_SINGLE)


def test_a_relayed_button_press_fires_once_not_once_per_advertisement(client):
    settings.button_devices = [{"id": MAC, "name": "Shelf",
                                "protocol": "bthome_button"}]
    # A real radio hears one press as a burst of identical advertisements, and
    # a burst can straddle two posts. One press must stay one press.
    packet = {"mac": MAC, "rssi": -55, "adv": BUTTON_ADV.hex()}
    first = _post(client, [packet, packet, packet])
    second = _post(client, [packet])
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["events"] + second.json()["events"] == 1

    live = gadgets_buttons.state_snapshot()["buttons"]
    entry = next(e for e in live if e["id"] == MAC)
    assert entry["battery"] == 100 and entry["via"] == "Cub Kitchen"


def test_an_unknown_button_arrives_as_discovered(client):
    r = _post(client, [{"mac": MAC, "rssi": -55, "adv": BUTTON_ADV.hex()}])
    assert r.status_code == 200 and r.json()["events"] == 0
    seen = gadgets_buttons.state_snapshot()["button_discovered"]
    assert any(e["id"] == MAC and e["protocol"] == "bthome_button" for e in seen)
