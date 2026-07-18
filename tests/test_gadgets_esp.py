"""ESPHome WiFi thermometer source (FoodAssistant-0oq3).

Covers the pure parsing (value/state Celsius+Fahrenheit, NaN "no reading yet",
device id, host normalization, validation, battery, the /events discovery
parser), the poll gating, and one mocked poll pass end to end into the gadgets
state. No network or ESP device needed.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import gadgets, gadgets_esp  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    gadgets.reset()
    yield
    gadgets.reset()


NOW = 1_700_000_000.0


# -- validation + normalization ----------------------------------------------

def test_valid_host_accepts_ip_and_mdns():
    assert gadgets_esp.valid_host("192.168.1.50")
    assert gadgets_esp.valid_host("fridge.local")
    assert gadgets_esp.valid_host("esp-kitchen")
    assert gadgets_esp.valid_host("192.168.1.50:8080")


def test_valid_host_rejects_scheme_path_space():
    assert not gadgets_esp.valid_host("http://192.168.1.50")
    assert not gadgets_esp.valid_host("192.168.1.50/sensor/x")
    assert not gadgets_esp.valid_host("has space")
    assert not gadgets_esp.valid_host("")


def test_normalize_host_strips_scheme_and_path():
    assert gadgets_esp.normalize_host("http://fridge.local/sensor/x") == "fridge.local"
    assert gadgets_esp.normalize_host("  192.168.1.50/  ") == "192.168.1.50"
    assert gadgets_esp.normalize_host("fridge.local.") == "fridge.local"


def test_valid_sensor():
    assert gadgets_esp.valid_sensor("fridge_temp")
    assert not gadgets_esp.valid_sensor("Fridge Temp")
    assert not gadgets_esp.valid_sensor("")


def test_device_id_is_stable_uppercase():
    a = gadgets_esp.device_id_for("Fridge.local", "fridge_temp")
    b = gadgets_esp.device_id_for("http://fridge.local/", "FRIDGE_TEMP")
    assert a == b == "ESP:FRIDGE.LOCAL:FRIDGE_TEMP"


# -- sensor_reading -----------------------------------------------------------

def test_sensor_reading_celsius():
    body = {"id": "sensor-fridge_temp", "value": 4.2, "state": "4.2 °C"}
    r = gadgets_esp.sensor_reading("192.168.1.50", "fridge_temp", body, NOW)
    assert r["id"] == "ESP:192.168.1.50:FRIDGE_TEMP"
    assert r["protocol"] == "esphome"
    assert r["probes"] == [{"index": 1, "temp_c": 4.2}]


def test_sensor_reading_fahrenheit_converted():
    body = {"id": "sensor-grill", "value": 212.0, "state": "212.0 °F"}
    r = gadgets_esp.sensor_reading("esp", "grill", body, NOW)
    assert r["probes"][0]["temp_c"] == 100.0


def test_sensor_reading_nan_is_none():
    body = {"id": "sensor-x", "value": float("nan"), "state": "nan °C"}
    assert gadgets_esp.sensor_reading("esp", "x", body, NOW) is None


def test_sensor_reading_non_numeric_is_none():
    assert gadgets_esp.sensor_reading("esp", "x", {"value": "n/a"}, NOW) is None
    assert gadgets_esp.sensor_reading("esp", "x", None, NOW) is None


def test_sensor_reading_battery_clamped():
    body = {"value": 3.0, "state": "3.0 °C"}
    r = gadgets_esp.sensor_reading("esp", "x", body, NOW, battery=150)
    assert r["battery"] == 100
    r2 = gadgets_esp.sensor_reading("esp", "x", body, NOW, battery="bad")
    assert r2["battery"] is None


def test_battery_pct_from():
    assert gadgets_esp.battery_pct_from(87.4, "87.4 %") == 87
    assert gadgets_esp.battery_pct_from(-5, "") == 0
    assert gadgets_esp.battery_pct_from("x", "") is None
    assert gadgets_esp.battery_pct_from(float("nan"), "") is None


# -- discovery parser ---------------------------------------------------------

def test_sensor_object_id_strips_domain():
    assert gadgets_esp.sensor_object_id("sensor-fridge_temp") == "fridge_temp"
    assert gadgets_esp.sensor_object_id("fridge_temp") == "fridge_temp"


def test_parse_events_stream_keeps_temperature_only():
    stream = (
        "event: state\n"
        'data: {"id":"sensor-fridge_temp","value":4.2,"state":"4.2 °C","name":"Fridge"}\n'
        "\n"
        "event: state\n"
        'data: {"id":"sensor-uptime","value":123,"state":"123 s","name":"Uptime"}\n'
        "\n"
        "event: state\n"
        'data: {"id":"sensor-fridge_temp","value":4.3,"state":"4.3 °C","name":"Fridge"}\n'
    )
    out = gadgets_esp.parse_events_stream(stream)
    assert len(out) == 1
    assert out[0]["sensor"] == "fridge_temp"
    assert out[0]["name"] == "Fridge"


def test_parse_events_stream_ignores_junk_lines():
    assert gadgets_esp.parse_events_stream("garbage\ndata: not json\n") == []


# -- settings glue + gating ---------------------------------------------------

def test_configured_devices_sanitizes(monkeypatch):
    monkeypatch.setattr(settings, "gadget_esp_devices", [
        {"host": "192.168.1.50", "sensor": "fridge_temp", "name": "Fridge"},
        {"host": "http://esp/", "sensor": "BAD SENSOR"},        # bad sensor
        {"host": "192.168.1.50", "sensor": "fridge_temp"},       # dup id
        {"nope": 1},                                             # junk
    ], raising=False)
    devs = gadgets_esp.configured_devices()
    assert len(devs) == 1
    assert devs[0]["host"] == "192.168.1.50"


def test_source_active_gating(monkeypatch):
    monkeypatch.setattr(settings, "gadget_esp_enabled", False, raising=False)
    monkeypatch.setattr(settings, "gadget_esp_devices",
                        [{"host": "esp", "sensor": "t"}], raising=False)
    assert not gadgets_esp.source_active()
    monkeypatch.setattr(settings, "gadget_esp_enabled", True, raising=False)
    assert gadgets_esp.source_active()
    monkeypatch.setattr(settings, "gadget_esp_devices", [], raising=False)
    assert not gadgets_esp.source_active()


# -- one mocked poll pass end to end -----------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, bodies):
        self._bodies = bodies

    async def get(self, url, auth=None):
        for key, payload in self._bodies.items():
            if url.endswith(key):
                return _FakeResp(payload)
        r = _FakeResp(None)
        r.status_code = 404
        return r


def test_poll_once_ingests(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "gadgets_enabled", True, raising=False)
    monkeypatch.setattr(settings, "gadget_esp_enabled", True, raising=False)
    dev_id = gadgets_esp.device_id_for("192.168.1.50", "fridge_temp")
    monkeypatch.setattr(settings, "gadget_esp_devices", [
        {"host": "192.168.1.50", "sensor": "fridge_temp", "name": "Fridge"},
    ], raising=False)
    monkeypatch.setattr(settings, "gadget_devices", [
        {"id": dev_id, "name": "Fridge", "protocol": "esphome", "targets": {}},
    ], raising=False)
    client = _FakeClient({"/sensor/fridge_temp":
                          {"value": 4.5, "state": "4.5 °C"}})
    n = asyncio.run(gadgets_esp.poll_once(client=client, now=NOW))
    assert n == 1
    state = gadgets.get_state(NOW)
    dev = {d["id"]: d for d in state["devices"]}[dev_id]
    assert dev["probes"][0]["temp_c"] == 4.5
    # ESP readings are not the Bluetooth reader: no reader heartbeat.
    assert state["reader_age_seconds"] is None


# -- ESP button -> action (FoodAssistant-k4wc) --------------------------------

def test_esp_action_button_starts_timer(monkeypatch, tmp_path):
    """An ESP button posting a timer token starts a shared kitchen timer through
    the same fire_key path the Start Page uses."""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    from app.routers.gadgets import esp_action, EspActionIn
    from app.services import timers
    timers.clear_all()
    out = asyncio.run(esp_action(EspActionIn(button="timer_eggs")))
    assert out["ok"] is True
    labels = [t["label"] for t in timers.list_timers()]
    assert "Eggs" in labels
    timers.clear_all()


def test_esp_action_long_press_resets_timer(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    from app.routers.gadgets import esp_action, EspActionIn
    from app.services import timers
    timers.clear_all()
    asyncio.run(esp_action(EspActionIn(button="timer_pasta")))
    assert any(t["label"] == "Pasta" for t in timers.list_timers())
    asyncio.run(esp_action(EspActionIn(button="timer_pasta", long=True)))
    assert not any(t["label"] == "Pasta" for t in timers.list_timers())
    timers.clear_all()


def test_esp_action_empty_and_unknown_are_reported():
    from app.routers.gadgets import esp_action, EspActionIn
    empty = asyncio.run(esp_action(EspActionIn(button="   ")))
    assert empty["ok"] is False
    unknown = asyncio.run(esp_action(EspActionIn(button="not_a_real_token")))
    assert unknown["ok"] is False


# -- ESP screen (display-out) -------------------------------------------------

def test_screen_mmss():
    assert gadgets_esp._screen_mmss(0) == "0:00"
    assert gadgets_esp._screen_mmss(65) == "1:05"
    assert gadgets_esp._screen_mmss(3661) == "1:01:01"
    assert gadgets_esp._screen_mmss(-5) == "0:00"
    assert gadgets_esp._screen_mmss("bad") == "0:00"


def test_compose_screen_orders_done_then_running_then_recipe():
    timers = [
        {"label": "Pasta", "remaining_seconds": 300, "expired": False},
        {"label": "Eggs", "remaining_seconds": 0, "expired": True},
        {"label": "Rice", "remaining_seconds": 90, "expired": False},
    ]
    lines = gadgets_esp.compose_screen(timers, "Carbonara", max_lines=4)
    assert lines[0] == "Eggs DONE"          # finished first
    assert lines[1] == "Rice 1:30"          # soonest running next
    assert lines[2] == "Pasta 5:00"
    assert lines[3] == "Cook: Carbonara"


def test_compose_screen_caps_lines_and_trims_labels():
    timers = [{"label": "A really long timer name here",
               "remaining_seconds": 120, "expired": False}]
    lines = gadgets_esp.compose_screen(timers, "", max_lines=1)
    assert len(lines) == 1
    assert lines[0].startswith("A really long ")  # trimmed to 14 chars + time


def test_compose_screen_never_blank():
    assert gadgets_esp.compose_screen([], "") == ["Pantry Raider"]


def test_esp_screen_endpoint(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    from app.routers.gadgets import esp_screen
    from app.services import timers, current_recipe
    timers.clear_all()
    current_recipe.clear_active()
    timers.create_timer("Eggs", 360)
    out = asyncio.run(esp_screen(lines=4))
    assert out["ok"] is True
    assert any("Eggs" in ln for ln in out["lines"])
    assert out["timers"][0]["label"] == "Eggs"
    timers.clear_all()
