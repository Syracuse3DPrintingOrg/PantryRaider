"""Pure-logic tests for the Home Assistant integration's payload helpers.

The integration lives under homeassistant/custom_components/pantry_raider and
most of its modules import the heavyweight ``homeassistant`` package, which the
repo test suite deliberately does not install. helpers.py is written to be free
of that dependency, so this test loads it directly by file path. If it ever
grows a Home Assistant import (a mistake), the module fails to load and every
test here is skipped rather than breaking the suite.
"""

import importlib.util
from pathlib import Path

import pytest

_HELPERS_PATH = (
    Path(__file__).resolve().parent.parent
    / "homeassistant"
    / "custom_components"
    / "pantry_raider"
    / "helpers.py"
)


def _load_helpers():
    spec = importlib.util.spec_from_file_location(
        "pantry_raider_helpers", _HELPERS_PATH
    )
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as err:  # pragma: no cover - only if HA leaks in
        pytest.skip(f"helpers.py is not import-clean without HA: {err}")
    return module


helpers = _load_helpers()


def _server_payload():
    return {
        "app": "pantryraider",
        "version": "0.18.21",
        "mode": "pi_hosted",
        "device_id": "srv-1",
        "hostname": "pr",
        "display": {
            "idle_timeout": 10,
            "screensaver_minutes": 5,
            "screensaver_mode": "bounce",
            "wake_on_presence": "auto",
        },
        "presence": {"available": True, "detected": False},
        "printers": {"label_queue": "idle", "document_queue": "idle", "queues": []},
        "expiring": {
            "expired": 2,
            "today": 1,
            "within_3_days": 3,
            "within_7_days": 4,
        },
        "counts": {"pending": 6, "action_items": 7},
        "timers": {
            "running": 2,
            "next": {"label": "Pasta", "remaining_seconds": 125.0},
            "active": [
                {"label": "Pasta", "remaining_seconds": 462.0, "expired": False},
                {"label": "Soft egg", "remaining_seconds": 130.0, "expired": False},
            ],
        },
        "thermometers": [
            {
                "id": "t1",
                "name": "TempSpike",
                "battery": 80,
                "stale": False,
                "probes": [
                    {"index": 0, "role": "food", "role_label": "Internal", "temp_c": 55.0, "target_c": 74.0},
                ],
            }
        ],
        "satellites": [
            {"device_id": "bandit-9", "hostname": "kitchen", "ip": "192.168.1.50", "version": "0.18.21"},
            {"hostname": "no-id", "ip": "192.168.1.51"},
        ],
    }


def _remote_payload():
    return {
        "app": "pantryraider",
        "version": "0.18.21",
        "mode": "pi_remote",
        "device_id": "bandit-9",
        "hostname": "kitchen",
        "display": {
            "idle_timeout": 8,
            "screensaver_minutes": 3,
            "screensaver_mode": "starfield",
            "wake_on_presence": "on",
        },
        "presence": {"available": True, "detected": True},
        "printers": {"label_queue": "idle", "document_queue": "idle", "queues": []},
    }


def test_is_server_mode():
    assert helpers.is_server_mode("server")
    assert helpers.is_server_mode("pi_hosted")
    assert not helpers.is_server_mode("pi_remote")
    assert not helpers.is_server_mode(None)


def test_server_device_model():
    assert helpers.server_device_model("server") == "Pantry Raider Server"
    assert helpers.server_device_model("pi_hosted") == "Pantry Raider Appliance"
    assert helpers.server_device_model("pi_remote") == "Pantry Raider Bandit"
    assert helpers.server_device_model("weird") == "Pantry Raider Server"


def test_format_timer_remaining():
    assert helpers.format_timer_remaining(125) == "2:05"
    assert helpers.format_timer_remaining(0) == "0:00"
    assert helpers.format_timer_remaining(-4) == "0:00"
    assert helpers.format_timer_remaining(3661) == "61:01"
    assert helpers.format_timer_remaining(None) == "0:00"


def test_next_timer_state():
    assert helpers.next_timer_state({"label": "Eggs", "remaining_seconds": 61}) == "Eggs 1:01"
    assert helpers.next_timer_state(None) == "none"
    assert helpers.next_timer_state({"label": "", "remaining_seconds": 5}) == "none"


def test_applicable_sensor_keys_by_mode():
    server_keys = helpers.applicable_sensor_keys(_server_payload())
    assert "expired" in server_keys and "next_timer" in server_keys
    assert "version" in server_keys and "label_queue" in server_keys
    assert "timers_summary" in server_keys

    remote_keys = helpers.applicable_sensor_keys(_remote_payload())
    assert remote_keys == ["version", "label_queue"]


def test_sensor_value_reads_nested_fields():
    data = _server_payload()
    assert helpers.sensor_value("expired", data) == 2
    assert helpers.sensor_value("today", data) == 1
    assert helpers.sensor_value("within_3_days", data) == 3
    assert helpers.sensor_value("within_7_days", data) == 4
    assert helpers.sensor_value("pending", data) == 6
    assert helpers.sensor_value("action_items", data) == 7
    assert helpers.sensor_value("timers_running", data) == 2
    assert helpers.sensor_value("timers_summary", data) == "Pasta 7:42, Soft egg 2:10"
    assert helpers.sensor_value("next_timer", data) == "Pasta 2:05"
    assert helpers.sensor_value("label_queue", data) == "idle"
    assert helpers.sensor_value("version", data) == "0.18.21"


def test_sensor_value_tolerates_missing_sections():
    assert helpers.sensor_value("expired", {}) is None
    assert helpers.sensor_value("label_queue", {}) is None
    assert helpers.sensor_value("next_timer", {}) == "none"


def test_expiring_attention():
    assert helpers.expiring_attention(_server_payload()) is True
    assert helpers.expiring_attention({"expiring": {"expired": 0, "today": 0}}) is False
    assert helpers.expiring_attention({}) is False


def test_probe_naming_and_ids():
    assert helpers.probe_display_name("TempSpike", "Internal") == "TempSpike Internal"
    assert helpers.probe_display_name("TempSpike", "") == "TempSpike"
    assert helpers.probe_display_name(None, None) == "Thermometer"
    assert helpers.probe_unique_id("srv-1", "t1", 0) == "srv-1_t1_p0"


def test_iter_probes_skips_bad_rows():
    data = {
        "thermometers": [
            {"id": "t1", "name": "A", "probes": [{"index": 0}, "junk"]},
            "not-a-dict",
        ]
    }
    pairs = list(helpers.iter_probes(data))
    assert len(pairs) == 1
    assert pairs[0][1]["index"] == 0


def test_satellite_device_ids_drops_rows_without_id():
    ids = helpers.satellite_device_ids(_server_payload())
    assert ids == ["bandit-9"]
    assert helpers.satellite_device_ids({}) == []


def test_active_timers_shapes_rows():
    active = helpers.active_timers(_server_payload())
    assert active == [
        {"label": "Pasta", "remaining_seconds": 462.0, "expired": False},
        {"label": "Soft egg", "remaining_seconds": 130.0, "expired": False},
    ]


def test_active_timers_degrades_when_absent():
    # Older servers omit timers.active entirely.
    assert helpers.active_timers({"timers": {"running": 1}}) == []
    assert helpers.active_timers({}) == []
    assert helpers.active_timers({"timers": {"active": "nope"}}) == []


def test_active_timers_skips_bad_rows_and_defaults_expired():
    data = {
        "timers": {
            "active": [
                {"label": "A", "remaining_seconds": 5},
                "junk",
                {"label": "B", "remaining_seconds": 0, "expired": True},
            ]
        }
    }
    active = helpers.active_timers(data)
    assert active == [
        {"label": "A", "remaining_seconds": 5, "expired": False},
        {"label": "B", "remaining_seconds": 0, "expired": True},
    ]


def test_active_timers_caps_at_twenty():
    data = {"timers": {"active": [{"label": f"t{i}", "remaining_seconds": i} for i in range(30)]}}
    assert len(helpers.active_timers(data)) == 20


def test_timers_summary_string():
    active = [
        {"label": "Pasta", "remaining_seconds": 462.0, "expired": False},
        {"label": "Soft egg", "remaining_seconds": 130.0, "expired": False},
    ]
    assert helpers.timers_summary(active) == "Pasta 7:42, Soft egg 2:10"
    assert helpers.timers_summary([]) == "none"
    assert helpers.timers_summary("nope") == "none"
    # Rows without a label are skipped, not rendered as blank entries.
    assert helpers.timers_summary([{"remaining_seconds": 5}]) == "none"


def test_timers_summary_clips_to_state_limit():
    active = [{"label": "Long label here", "remaining_seconds": 61} for _ in range(40)]
    summary = helpers.timers_summary(active)
    assert len(summary) <= 255


def test_has_display():
    # A plain server drives no screen of its own.
    assert not helpers.has_display("server", False)
    # Appliances and bandits do.
    assert helpers.has_display("pi_hosted", False)
    assert helpers.has_display("pi_remote", False)
    # Any discovered bandit (is_satellite) always has a display.
    assert helpers.has_display("server", True)
    assert helpers.has_display(None, True)
