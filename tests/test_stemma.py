"""Plug-in STEMMA QT / Qwiic accessories (FoodAssistant-etsc, -kh1m).

The NeoKey 1x4 is the launch device: four keys that select the barcode
scanner's mode, with LEDs showing which mode is live. Covered here, all pure
(no bus, no hardware, no network): the discovery table and its collision
cases resolved from canned probe answers, the seesaw register decode and
hardware-id handshake, the GPIO bitmask to key-index decode with its edge and
repeat rules, the key-to-mode mapping, the LED derivation, the outputs
endpoint's shape, the registry CRUD and config plumbing, and the settings
pane render.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
SERVICE = _ROOT / "service"
sys.path.insert(0, str(SERVICE))
sys.path.insert(0, str(_ROOT / "gadgets"))

from foodassistant_gadgets import config as gd_config  # noqa: E402
from foodassistant_gadgets.i2c import discovery, seesaw  # noqa: E402
from foodassistant_gadgets.i2c.drivers import neokey  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import gadgets, stemma  # noqa: E402
from app.services.scanner_mode import SCANNER_MODES  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    gadgets.reset()
    yield
    gadgets.reset()


# -- Discovery: the address table and its collisions ---------------------------

def test_address_table_covers_the_launch_devices():
    # Every NeoKey jumper setting is probed, not just the default 0x30.
    for address in neokey.ADDRESSES:
        assert "neokey" in discovery.candidates_for(address)
    # The documented phase-1 sensors are in the table even before their
    # drivers land, so a plugged-in board is never silently invisible.
    assert "aht20" in discovery.candidates_for(0x38)
    assert "sht4x" in discovery.candidates_for(0x44)
    assert "apds9960" in discovery.candidates_for(0x39)
    for address in range(0x18, 0x20):
        assert "mcp9808" in discovery.candidates_for(address)


def test_unprobed_address_has_no_candidates():
    # 0x50 (an EEPROM) is not on the list: the sweep touches known addresses
    # only, never a blind range scan.
    assert discovery.candidates_for(0x50) == ()


def test_choose_resolves_the_neokey_from_a_confirmed_probe():
    answer = discovery.choose(0x30, {"neokey": "neokey"})
    assert answer == {"model": "neokey", "supported": True,
                      "name": "NeoKey 1x4"}


def test_choose_reports_a_silent_part_at_a_shared_address_as_unsupported():
    # 0x60 is the NeoDriver, a PCA9685, and the DRV8830. Something ACKed but
    # failed the seesaw handshake, so it is not a NeoDriver: report it seen
    # but undriveable rather than claiming it.
    answer = discovery.choose(0x60, {"neodriver": None})
    assert answer["supported"] is False
    assert answer["model"] == "neodriver"  # the likeliest candidate, named


def test_choose_prefers_the_first_matching_candidate_at_a_collision():
    # 0x29 is both ToF sensors; the L1X is listed first and wins when both
    # somehow answer, and the L0X is reported (unsupported) when only it does.
    assert discovery.candidates_for(0x29) == ("vl53l1x", "vl53l0x")
    answer = discovery.choose(0x29, {"vl53l1x": "vl53l1x", "vl53l0x": "vl53l0x"})
    assert answer["model"] == "vl53l1x" and answer["supported"] is True


def test_choose_on_an_unknown_address_is_unsupported_and_unnamed():
    answer = discovery.choose(0x50, {})
    assert answer == {"model": "", "supported": False,
                      "name": "Unknown accessory"}


def test_device_id_is_bus_plus_address_and_round_trips():
    assert discovery.device_id(1, 0x30) == "i2c:1:0x30"
    assert stemma.parse_device_id("i2c:1:0x30") == (1, 0x30)
    assert stemma.device_id(1, 0x30) == "i2c:1:0x30"
    # The agent's id and the app's id are the same string, or the registry
    # would never match a discovered board to a configured one.
    assert discovery.device_id(1, 0x33) == stemma.device_id(1, 0x33)


def test_parse_device_id_rejects_junk():
    for bad in ("", None, "AA:BB:CC:DD:EE:FF", "i2c:1:0xZZ", "i2c:0x30",
                "spi:1:0x30"):
        assert stemma.parse_device_id(bad) is None


# -- A fake bus, so the sweep and the drivers run with no hardware -------------

class FakeBus:
    """Canned register answers keyed by (address, base, function)."""

    def __init__(self, devices: dict):
        self.devices = devices          # address -> {(base, function): bytes}
        self.available = True
        self.detail = ""
        self.writes: list = []
        self._pending: tuple | None = None

    def ping(self, address):
        return address in self.devices

    def write_bytes(self, address, data):
        self.writes.append((address, bytes(data)))
        if len(data) >= 2:
            # A seesaw read is a two-byte [base, function] write followed by a
            # plain read, so the last command written is what the next read
            # answers.
            self._pending = (data[0], data[1])

    def read_bytes(self, address, length):
        answer = self.devices.get(address, {}).get(self._pending, b"")
        return bytes(answer[:length]).ljust(length, b"\x00")


def _neokey_registers():
    """A seesaw board that answers like a NeoKey: an ATtiny817 hardware id,
    and a module inventory carrying GPIO and NeoPixel."""
    options = (1 << seesaw.GPIO_BASE) | (1 << seesaw.NEOPIXEL_BASE)
    return {
        (seesaw.STATUS_BASE, seesaw.STATUS_HW_ID): bytes([0x87]),
        (seesaw.STATUS_BASE, seesaw.STATUS_OPTIONS): options.to_bytes(4, "big"),
        # All four keys up (pins 4-7 pulled high).
        (seesaw.GPIO_BASE, seesaw.GPIO_BULK): (0xF0).to_bytes(4, "big"),
    }


def test_sweep_finds_a_neokey_and_reports_it_in_the_push_shape():
    bus = FakeBus({0x30: _neokey_registers()})
    found = discovery.sweep(bus, 1)
    assert len(found) == 1
    assert found[0] == {"id": "i2c:1:0x30", "kind": "stemma",
                        "model": "neokey", "name": "NeoKey 1x4",
                        "address": "0x30", "supported": True}


def test_sweep_reports_a_known_address_holding_something_undriveable():
    # An AHT20 answers at 0x38, and there is no driver for it yet. It must
    # still ride the discovered list, marked unsupported.
    bus = FakeBus({0x38: {}})
    found = discovery.sweep(bus, 1)
    assert len(found) == 1
    assert found[0]["model"] == "aht20" and found[0]["supported"] is False


def test_sweep_leaves_a_non_seesaw_part_at_a_neokey_address_unsupported():
    # Something answers at 0x31 but fails the hardware-id handshake (its id
    # byte is not a seesaw chip), so it must not be claimed as a NeoKey.
    bus = FakeBus({0x31: {
        (seesaw.STATUS_BASE, seesaw.STATUS_HW_ID): bytes([0x11]),
    }})
    found = discovery.sweep(bus, 1)
    assert found[0]["supported"] is False


def test_probe_rejects_a_seesaw_without_the_neopixel_module():
    # A seesaw build with GPIO but no LEDs is not a NeoKey. The module
    # inventory is the second half of the handshake for exactly this reason.
    registers = _neokey_registers()
    registers[(seesaw.STATUS_BASE, seesaw.STATUS_OPTIONS)] = \
        (1 << seesaw.GPIO_BASE).to_bytes(4, "big")
    bus = FakeBus({0x30: registers})
    assert neokey.probe(bus, 0x30) is None


def test_probe_accepts_a_neokey_on_either_chip_generation():
    # NeoKeys have shipped on the SAMD09 and on the ATtiny817; a probe that
    # only knew one would call half the boards in the wild unsupported.
    for hw_id in (0x55, 0x87):
        registers = _neokey_registers()
        registers[(seesaw.STATUS_BASE, seesaw.STATUS_HW_ID)] = bytes([hw_id])
        assert neokey.probe(FakeBus({0x30: registers}), 0x30) == "neokey"


# -- Seesaw: register and hardware-id decode -----------------------------------

def test_decode_hw_id_names_the_known_chips_and_rejects_the_rest():
    assert seesaw.decode_hw_id(0x55) == "samd09"
    assert seesaw.decode_hw_id(0x87) == "attiny817"
    assert seesaw.decode_hw_id(0x00) == ""
    assert seesaw.decode_hw_id(0xFF) == ""
    assert seesaw.decode_hw_id(None) == ""


def test_decode_options_reads_the_module_inventory_bitmask():
    mask = (1 << seesaw.GPIO_BASE) | (1 << seesaw.NEOPIXEL_BASE)
    modules = seesaw.decode_options(mask.to_bytes(4, "big"))
    assert seesaw.GPIO_BASE in modules and seesaw.NEOPIXEL_BASE in modules
    assert seesaw.ENCODER_BASE not in modules
    # A short or empty answer is no inventory, never a crash.
    assert seesaw.decode_options(b"") == set()
    assert seesaw.decode_options(b"\x01") == set()


def test_decode_bulk_is_big_endian_32_bit():
    assert seesaw.decode_bulk((0xF0).to_bytes(4, "big")) == 0xF0
    assert seesaw.decode_bulk(bytes([0x80, 0x00, 0x00, 0x01])) == 0x80000001
    assert seesaw.decode_bulk(b"") == 0


def test_pin_mask_sets_a_bit_per_pin():
    assert seesaw.pin_mask((4, 5, 6, 7)) == 0xF0
    assert seesaw.pin_mask(()) == 0


def test_encode_pixel_puts_green_first():
    # NeoPixels take GRB on the wire; sending RGB would make every mode color
    # the wrong one.
    assert seesaw.encode_pixel((255, 0, 0)) == bytes([0, 255, 0])
    assert seesaw.encode_pixel((0, 200, 83)) == bytes([200, 0, 83])
    assert seesaw.encode_pixel((300, -5, 10)) == bytes([0, 255, 10])  # clamped


def test_encode_pixel_buffer_concatenates_the_strip():
    buf = seesaw.encode_pixel_buffer([(255, 0, 0), (0, 0, 255)])
    assert buf == bytes([0, 255, 0, 0, 0, 255])


# -- NeoKey: bitmask to key index, edges, repeats -------------------------------

def test_pressed_keys_decodes_the_active_low_bitmask():
    # Pins 4-7 pulled up: a bit at 1 is a key UP, a bit at 0 is a key DOWN.
    assert neokey.pressed_keys(0xF0) == ()          # all up
    assert neokey.pressed_keys(0xE0) == (0,)        # pin 4 low: key 1
    assert neokey.pressed_keys(0x70) == (3,)        # pin 7 low: key 4
    assert neokey.pressed_keys(0xA0) == (0, 2)      # pins 4 and 6 low
    assert neokey.pressed_keys(0x00) == (0, 1, 2, 3)


def test_pressed_keys_ignores_pins_outside_the_keys():
    # Other GPIO pins on the seesaw are not keys and must not read as one.
    assert neokey.pressed_keys(0xF0 | 0x0F) == ()
    assert neokey.pressed_keys(0xFFFFFFF0 & ~(1 << 5)) == (1,)


def test_pressed_keys_survives_junk():
    assert neokey.pressed_keys(None) == ()
    assert neokey.pressed_keys("nonsense") == ()


def test_key_events_fire_on_the_down_edge_only():
    # Nothing held, key 1 goes down: one event.
    assert neokey.key_events((), (0,)) == (0,)
    # Still held on the next scan: no repeat.
    assert neokey.key_events((0,), (0,)) == ()
    # Released: no event on the way up.
    assert neokey.key_events((0,), ()) == ()
    # A second key joins while the first is held: only the new one fires.
    assert neokey.key_events((0,), (0, 2)) == (2,)


def test_swallow_repeats_drops_a_fast_second_press_and_allows_a_later_one():
    seen = {}
    assert neokey.swallow_repeats(seen, (0,), 100.0) == (0,)
    # A bounce or a double-tap inside the window is not a second mode change.
    assert neokey.swallow_repeats(seen, (0,), 100.1) == ()
    # Past the window, the same key is a real press again.
    assert neokey.swallow_repeats(seen, (0,), 100.5) == (0,)
    # A different key is never swallowed by its neighbor's timer.
    assert neokey.swallow_repeats(seen, (1,), 100.51) == (1,)


# -- The key-to-mode mapping ----------------------------------------------------

def test_default_keymap_is_the_scanner_mode_order():
    assert stemma.default_keymap() == list(SCANNER_MODES[:4])
    assert stemma.default_keymap() == ["inventory", "consume", "shopping", "audit"]


def test_normalize_keymap_fills_defaults_and_drops_unknown_modes():
    # Nothing stored: the default order, so a NeoKey works the moment it is added.
    assert stemma.normalize_keymap(None) == stemma.default_keymap()
    assert stemma.normalize_keymap("nonsense") == stemma.default_keymap()
    # A saved mapping is kept as saved.
    saved = ["consume", "consume", "", "shopping"]
    assert stemma.normalize_keymap(saved) == saved
    # An unknown mode becomes "nothing" rather than landing on a real mode:
    # a typo must never make a key consume stock.
    assert stemma.normalize_keymap(["inventory", "bogus", "shopping", "audit"]) \
        == ["inventory", "", "shopping", "audit"]
    # Always exactly four entries, short or long.
    assert len(stemma.normalize_keymap(["consume"])) == 4
    assert len(stemma.normalize_keymap(["consume"] * 9)) == 4


def test_agent_and_app_normalize_a_keymap_the_same_way():
    # The agent re-normalizes what it pulls, so the two must agree or a key
    # would do one thing and the settings card would claim another.
    for raw in (None, ["consume", "bogus", "", "audit"], ["shopping"],
                ["inventory", "consume", "shopping", "audit"]):
        assert neokey.normalize_keymap(raw) == stemma.normalize_keymap(raw)


def test_mode_for_key_reads_the_mapping_by_index():
    keymap = ["inventory", "consume", "", "audit"]
    assert stemma.mode_for_key(keymap, 0) == "inventory"
    assert stemma.mode_for_key(keymap, 2) == ""
    assert stemma.mode_for_key(keymap, 9) == ""


def test_keymap_choices_offer_every_mode_plus_nothing():
    values = [c["value"] for c in stemma.keymap_choices()]
    assert values == ["", "inventory", "consume", "shopping", "audit"]


# -- LED derivation ------------------------------------------------------------

def test_mode_colors_cover_every_scanner_mode():
    # A mode with no color would light nothing when selected.
    for mode in SCANNER_MODES:
        assert mode in stemma.MODE_COLORS


def test_agent_palette_matches_the_apps():
    # The app owns the palette and ships it in the outputs payload; the agent
    # keeps a fallback for when the app is briefly unreachable. If these ever
    # drift, a NeoKey shows one color at boot and another a poll later.
    assert neokey.MODE_COLORS == {k: tuple(v) for k, v in stemma.MODE_COLORS.items()}
    assert neokey.IDLE_LED_SCALE == stemma.IDLE_LED_SCALE
    assert neokey.KEY_COUNT == stemma.NEOKEY_KEYS


def test_led_colors_light_the_active_key_and_dim_the_rest():
    colors = stemma.led_colors(stemma.default_keymap(), "consume", 100)
    # Key 2 is consume: full amber.
    assert colors[1] == (255, 145, 0)
    # The others glow faintly in their own colors, not off and not full.
    assert colors[0] == stemma.scale_color((0, 200, 83), stemma.IDLE_LED_SCALE)
    assert colors[0] != (0, 0, 0) and colors[0] != (0, 200, 83)


def test_led_colors_leave_an_unmapped_key_dark():
    colors = stemma.led_colors(["inventory", "", "shopping", "audit"],
                               "inventory", 100)
    assert colors[1] == (0, 0, 0)


def test_led_colors_scale_with_brightness():
    full = stemma.led_colors(["consume"], "consume", 100)[0]
    half = stemma.led_colors(["consume"], "consume", 50)[0]
    off = stemma.led_colors(["consume"], "consume", 0)[0]
    assert full == (255, 145, 0)
    assert half == (128, 72, 0)
    # Brightness 0 is a real choice (a dark keypad), not a fallback to default.
    assert off == (0, 0, 0)


def test_led_colors_light_both_keys_when_two_share_a_mode():
    # The user mapped two keys to the same mode; showing only one would be a
    # lie about their own configuration.
    colors = stemma.led_colors(["consume", "consume", "", ""], "consume", 100)
    assert colors[0] == colors[1] == (255, 145, 0)


def test_led_colors_with_an_unknown_active_mode_leave_everything_dim():
    colors = stemma.led_colors(stemma.default_keymap(), "", 100)
    assert all(c != (0, 0, 0) for c in colors)   # still readable
    assert (0, 200, 83) not in colors            # nothing claims to be active


def test_agent_and_app_derive_the_same_leds():
    for mode in list(SCANNER_MODES) + [""]:
        for brightness in (0, 40, 100):
            want = stemma.led_colors(stemma.default_keymap(), mode, brightness)
            got = neokey.led_colors(stemma.default_keymap(), mode, brightness)
            assert got == want, (mode, brightness)


def test_agent_leds_prefer_the_palette_the_app_sent():
    # The outputs payload carries the app's colors so a palette change on the
    # server reaches the keys without redeploying the agent.
    palette = {"consume": (1, 2, 3)}
    colors = neokey.led_colors(["consume"], "consume", 100, palette)
    assert colors[0] == (1, 2, 3)


def test_test_colors_light_one_key_white():
    frame = neokey.test_colors(2, 100)
    assert frame[2] == (255, 255, 255)
    assert frame[0] == frame[1] == frame[3] == (0, 0, 0)


# -- The outputs payload --------------------------------------------------------

def test_build_outputs_carries_the_mode_the_palette_and_the_timers():
    out = stemma.build_outputs(
        {"mode": "consume", "label": "Use"}, [],
        [{"remaining_seconds": 240, "expired": False},
         {"remaining_seconds": 900, "expired": False}])
    assert out["scanner_mode"] == "consume"
    assert out["scanner_label"] == "Use"
    assert out["mode_colors"]["consume"] == [255, 145, 0]
    assert out["alarm_active"] is False
    assert out["timer"] == {"ringing": False, "running": 2,
                            "soonest_remaining": 240}


def test_build_outputs_flags_a_ringing_timer_and_a_live_alarm():
    out = stemma.build_outputs(
        {"mode": "inventory"}, [{"key": "hygro:x", "message": "Too warm"}],
        [{"remaining_seconds": 0, "expired": True}])
    assert out["alarm_active"] is True
    assert out["timer"]["ringing"] is True
    # A ringing timer has nothing left to count down, so there is no soonest.
    assert out["timer"]["soonest_remaining"] is None


def test_build_outputs_falls_back_to_the_default_mode_on_junk():
    out = stemma.build_outputs({"mode": "bogus"}, [], [])
    assert out["scanner_mode"] == SCANNER_MODES[0]
    out = stemma.build_outputs({}, [], None)
    assert out["scanner_mode"] == SCANNER_MODES[0]
    assert out["timer"] == {"ringing": False, "running": 0,
                            "soonest_remaining": None}


def test_build_outputs_includes_a_pending_key_test_only_when_there_is_one():
    assert "key_test" not in stemma.build_outputs({"mode": "audit"}, [], [])
    out = stemma.build_outputs({"mode": "audit"}, [], [],
                               key_test={"id": "i2c:1:0x30", "key": 2,
                                         "ts": 1000.0})
    assert out["key_test"] == {"id": "i2c:1:0x30", "key": 2, "ts": 1000.0}


# -- Options and registry normalization -----------------------------------------

def test_normalize_brightness_clamps_and_defaults():
    assert stemma.normalize_brightness(None) == stemma.NEOKEY_DEFAULT_BRIGHTNESS
    assert stemma.normalize_brightness("x") == stemma.NEOKEY_DEFAULT_BRIGHTNESS
    assert stemma.normalize_brightness(0) == 0
    assert stemma.normalize_brightness(250) == 100
    assert stemma.normalize_brightness(-5) == 0
    assert stemma.normalize_brightness(37.6) == 38


def test_normalize_device_rejects_bad_ids_and_unknown_kinds():
    assert stemma.normalize_device({"id": "i2c:1:0x30", "kind": "neokey"})
    assert stemma.normalize_device({"id": "nope", "kind": "neokey"}) is None
    assert stemma.normalize_device({"id": "i2c:1:0x30", "kind": "toaster"}) is None
    assert stemma.normalize_device("nonsense") is None


def test_normalize_device_fills_the_default_options():
    dev = stemma.normalize_device({"id": "i2c:1:0x30", "kind": "neokey"})
    assert dev["options"] == {"keymap": stemma.default_keymap(),
                              "brightness": stemma.NEOKEY_DEFAULT_BRIGHTNESS}


# -- Agent config plumbing ------------------------------------------------------

def test_agent_config_has_an_i2c_opt_out_and_a_bus_override(tmp_path):
    cfg = gd_config.Config().validated()
    assert cfg.i2c is True and cfg.i2c_bus == 1
    path = tmp_path / "gadgets.toml"
    path.write_text("i2c = false\ni2c_bus = 3\n")
    loaded = gd_config.load(path)
    assert loaded.i2c is False and loaded.i2c_bus == 3


def test_i2c_module_applies_a_pulled_config():
    from foodassistant_gadgets.i2c.module import I2CModule
    mod = I2CModule(1)
    mod.apply_config({"enabled": True, "devices": [
        {"id": "I2C:1:0X30", "kind": "neokey", "name": "Counter keys"},
        {"no": "id"},
    ]})
    assert mod.enabled is True
    # Ids normalize to lowercase, so a hand-edited config still matches.
    assert list(mod.devices) == ["i2c:1:0x30"]
    # A device removed in the app stops being polled without a restart.
    mod.apply_config({"enabled": False, "devices": []})
    assert mod.devices == {} and mod.enabled is False
    # A missing block is not a crash: the class is simply off.
    mod.apply_config(None)
    assert mod.enabled is False


def test_i2c_module_outputs_do_not_stomp_a_fresh_press():
    import time as _time
    from foodassistant_gadgets.i2c.module import I2CModule
    mod = I2CModule(1)
    mod.apply_outputs({"scanner_mode": "audit"})
    assert mod._mode == "audit"
    # A press just happened; an outputs answer that was already in flight
    # must not snap the LED back to the old mode.
    mod._mode = "consume"
    mod._mode_from_press = _time.time()
    mod.apply_outputs({"scanner_mode": "audit"})
    assert mod._mode == "consume"
    # Once the press is old, the server is authoritative again.
    mod._mode_from_press = _time.time() - 10
    mod.apply_outputs({"scanner_mode": "audit"})
    assert mod._mode == "audit"


def test_i2c_module_reports_bus_health_without_a_bus():
    from foodassistant_gadgets.i2c.module import I2CModule
    # Bus 99 does not exist anywhere, which is the point: the module must
    # degrade with a reason rather than raise.
    mod = I2CModule(99)
    assert mod.bus.open() is False
    health = mod.health()
    assert health["available"] is False and health["detail"]


# -- The app endpoints ----------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd(); os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    # The setup-redirect middleware answers every request with the wizard
    # until the install is configured, so these two make it a real install.
    monkeypatch.setattr(settings, "grocy_base_url", "http://g", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "stemma_enabled", False, raising=False)
    monkeypatch.setattr(settings, "stemma_devices", [], raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_add_accessory_enables_the_class_and_maps_the_default_keys(client):
    r = client.post("/gadgets/stemma", json={
        "id": "i2c:1:0x30", "kind": "neokey", "name": "Counter keys"}).json()
    assert r["ok"] is True
    cfg = client.get("/gadgets/config").json()
    assert cfg["stemma"]["enabled"] is True
    dev = cfg["stemma"]["devices"][0]
    assert dev["id"] == "i2c:1:0x30" and dev["name"] == "Counter keys"
    # Usable the moment it is added: the default order is already mapped.
    assert dev["options"]["keymap"] == ["inventory", "consume", "shopping", "audit"]
    # The Bluetooth classes are untouched.
    assert cfg["enabled"] is False and cfg["devices"] == []


def test_add_accessory_rejects_a_bad_id_or_an_unsupported_board(client):
    r = client.post("/gadgets/stemma", json={"id": "nope", "kind": "neokey"}).json()
    assert r["ok"] is False and "i2c:1:0x30" in r["error"]
    r = client.post("/gadgets/stemma",
                    json={"id": "i2c:1:0x38", "kind": "aht20"}).json()
    assert r["ok"] is False


def test_re_adding_an_accessory_updates_rather_than_duplicating(client):
    client.post("/gadgets/stemma", json={"id": "i2c:1:0x30", "kind": "neokey",
                                         "name": "Keys"})
    client.post("/gadgets/stemma/edit", json={"device_id": "i2c:1:0x30",
                                              "keymap": ["audit", "", "", ""]})
    client.post("/gadgets/stemma", json={"id": "i2c:1:0x30", "kind": "neokey",
                                         "name": "Counter keys"})
    devices = client.get("/gadgets/config").json()["stemma"]["devices"]
    assert len(devices) == 1 and devices[0]["name"] == "Counter keys"
    # Re-adding a board already there must not throw its mapping away.
    assert devices[0]["options"]["keymap"] == ["audit", "", "", ""]


def test_edit_accessory_applies_only_the_fields_sent(client):
    client.post("/gadgets/stemma", json={"id": "i2c:1:0x30", "kind": "neokey"})
    r = client.post("/gadgets/stemma/edit", json={
        "device_id": "i2c:1:0x30",
        "keymap": ["consume", "consume", "bogus", ""],
        "brightness": 80}).json()
    assert r["ok"] is True
    dev = client.get("/gadgets/config").json()["stemma"]["devices"][0]
    assert dev["options"]["keymap"] == ["consume", "consume", "", ""]
    assert dev["options"]["brightness"] == 80
    # A rename leaves the mapping and the brightness alone.
    client.post("/gadgets/stemma/edit", json={"device_id": "i2c:1:0x30",
                                              "name": "Counter keys"})
    dev = client.get("/gadgets/config").json()["stemma"]["devices"][0]
    assert dev["name"] == "Counter keys"
    assert dev["options"]["keymap"] == ["consume", "consume", "", ""]
    assert dev["options"]["brightness"] == 80
    # Unknown device reports, never raises.
    assert client.post("/gadgets/stemma/edit",
                       json={"device_id": "i2c:1:0x99", "name": "X"}
                       ).json()["ok"] is False


def test_remove_accessory(client):
    client.post("/gadgets/stemma", json={"id": "i2c:1:0x30", "kind": "neokey"})
    assert client.delete("/gadgets/stemma/i2c:1:0x30").json()["ok"] is True
    assert client.get("/gadgets/config").json()["stemma"]["devices"] == []


def test_readings_push_routes_a_heartbeat_and_a_discovery(client):
    client.post("/gadgets/stemma", json={"id": "i2c:1:0x30", "kind": "neokey",
                                         "name": "Counter keys"})
    client.post("/gadgets/readings", json={
        "devices": [{"id": "i2c:1:0x30", "kind": "stemma", "model": "neokey",
                     "name": "Counter keys"}],
        "discovered": [{"id": "i2c:1:0x38", "kind": "stemma", "model": "aht20",
                        "name": "AHT20 temperature and humidity",
                        "address": "0x38", "supported": False}],
        "i2c": {"available": True, "detail": ""},
    })
    state = client.get("/gadgets/state").json()
    assert state["stemma_enabled"] is True
    dev = state["stemma"][0]
    assert dev["name"] == "Counter keys" and dev["stale"] is False
    assert dev["address"] == "0x30"
    # The card carries the key layout with its colors, ready to render.
    assert [k["mode"] for k in dev["keys"]] == ["inventory", "consume",
                                                "shopping", "audit"]
    assert dev["keys"][0]["color"] == [0, 200, 83]
    # The unsupported board is listed, so a missing driver reads differently
    # from a bad cable.
    found = state["stemma_discovered"][0]
    assert found["model"] == "aht20" and found["supported"] is False
    # No cross-contamination into the Bluetooth classes.
    assert state["devices"] == [] and state["hygrometers"] == []


def test_state_reports_an_unusable_bus_so_the_pane_can_say_why(client):
    client.post("/gadgets/readings", json={
        "devices": [],
        "i2c": {"available": False,
                "detail": "/dev/i2c-1 is not there, so this device has no I2C bus turned on."},
    })
    state = client.get("/gadgets/state").json()
    assert state["i2c_available"] is False
    assert "/dev/i2c-1" in state["i2c_detail"]


def test_a_configured_accessory_leaves_the_discovered_list(client):
    client.post("/gadgets/readings", json={"discovered": [
        {"id": "i2c:1:0x30", "kind": "stemma", "model": "neokey",
         "name": "NeoKey 1x4", "address": "0x30", "supported": True}]})
    assert client.get("/gadgets/state").json()["stemma_discovered"]
    client.post("/gadgets/stemma", json={"id": "i2c:1:0x30", "kind": "neokey"})
    assert client.get("/gadgets/state").json()["stemma_discovered"] == []


def test_outputs_endpoint_answers_with_the_mode_and_the_palette(client):
    r = client.get("/gadgets/outputs").json()
    assert r["scanner_mode"] == "inventory"
    assert r["mode_colors"]["audit"] == [170, 0, 255]
    assert r["alarm_active"] is False
    # The mode a NeoKey sets is the mode the outputs poll reports back, which
    # is what keeps every other surface's LEDs in step.
    client.post("/pending/scanner-mode", json={"mode": "shopping",
                                               "source": "neokey"})
    assert client.get("/gadgets/outputs").json()["scanner_mode"] == "shopping"


def test_key_test_is_queued_for_the_agent_and_ages_out(client):
    client.post("/gadgets/stemma", json={"id": "i2c:1:0x30", "kind": "neokey"})
    r = client.post("/gadgets/stemma/test",
                    json={"device_id": "i2c:1:0x30", "key": 2}).json()
    assert r["ok"] is True
    out = client.get("/gadgets/outputs").json()
    assert out["key_test"]["id"] == "i2c:1:0x30" and out["key_test"]["key"] == 2
    # Stale requests are dropped, so a click never flashes a key a minute later.
    assert gadgets.stemma_key_test(now=out["key_test"]["ts"]
                                   + gadgets.STEMMA_TEST_TTL + 1) is None
    # Unknown device or key reports, never raises.
    assert client.post("/gadgets/stemma/test",
                       json={"device_id": "i2c:1:0x99"}).json()["ok"] is False
    assert client.post("/gadgets/stemma/test",
                       json={"device_id": "i2c:1:0x30", "key": 9}
                       ).json()["ok"] is False


# -- The scanner-mode toast -----------------------------------------------------

def test_a_physical_key_press_toasts_the_new_mode(client):
    from app.services import ha_events
    ha_events.reset()
    client.post("/pending/scanner-mode", json={"mode": "consume",
                                               "source": "neokey"})
    events = ha_events.poll(0)["events"]
    assert len(events) == 1
    assert events[0]["message"] == "Scanner mode: Use"


def test_a_kiosk_mode_change_stays_quiet(client):
    # The kiosk and the deck already show the mode on screen, so a toast
    # would be noise. Only a control with no display of its own gets one.
    from app.services import ha_events
    ha_events.reset()
    client.post("/pending/scanner-mode", json={"mode": "consume"})
    assert ha_events.poll(0)["events"] == []
    client.post("/pending/scanner-mode/cycle")
    assert ha_events.poll(0)["events"] == []


# -- Settings plumbing and the pane render --------------------------------------

def test_stemma_settings_are_saveable_and_default_off():
    from app.config import Settings, _SAVEABLE
    from app.routers.setup import SetupPayload
    for key in ("stemma_enabled", "stemma_devices"):
        assert key in Settings.model_fields, f"{key} missing from Settings"
        assert key in _SAVEABLE, f"{key} missing from _SAVEABLE"
    # The toggle saves from the pane; the device list is managed by the
    # /gadgets endpoints, the same split the other gadget classes use.
    assert "stemma_enabled" in SetupPayload.model_fields
    assert "stemma_devices" not in SetupPayload.model_fields
    assert Settings.model_fields["stemma_enabled"].default is False
    assert Settings.model_fields["stemma_devices"].default == []


def test_stemma_config_is_not_pulled_from_a_main_server():
    # A QT board is plugged into ONE device, so its registry is that device's
    # even on a satellite whose Bluetooth sensors are managed on the server.
    from app.config import SATELLITE_PULL_FIELDS
    assert "stemma_devices" not in SATELLITE_PULL_FIELDS
    assert "stemma_enabled" not in SATELLITE_PULL_FIELDS


def test_the_gadgets_pane_renders_the_accessories_section(client):
    from unittest.mock import patch
    with patch.object(type(settings), "is_configured", lambda self: True):
        html = client.get("/setup").text
    assert 'id="gadget-sec-stemma"' in html
    assert "gadgetsShowSection('stemma')" in html
    assert 'id="stemma_enabled"' in html
    assert 'id="stemma-devices"' in html
    assert 'id="stemma-discovered"' in html
    assert "Plug-in accessories (STEMMA QT / Qwiic)" in html


def test_the_pane_js_mode_choices_match_the_server():
    # The mapping dropdown is built client-side; if a mode is added to
    # SCANNER_MODES and not here, the editor silently cannot select it.
    js = (SERVICE / "app" / "static" / "js" / "setup" / "panes.js").read_text()
    block = js.split("_STEMMA_MODE_CHOICES = [")[1].split("];")[0]
    for mode in SCANNER_MODES:
        assert f"value: '{mode}'" in block, f"{mode} missing from the editor"
