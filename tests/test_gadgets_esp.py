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
