"""Satellite gadget relay (FoodAssistant-me3t).

A satellite forwards every gadget push (readings, discoveries, button
presses, door events) to its main server, tagged with its own name, so
sensors near any satellite appear on the server and are managed there.
Covers the pure payload shaping and merging, the queue behavior, the
server-side source tagging and dedupe, the satellite-side alarm and button
suppression, and the merged reader config. No network: the upstream POST is
always mocked.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SERVICE = REPO / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import gadgets, gadgets_buttons, gadgets_relay, ha_events  # noqa: E402
from app.services import satellite as satellite_svc  # noqa: E402
from app.routers import gadgets as gadgets_router  # noqa: E402


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    gadgets.reset()
    gadgets_buttons.reset()
    gadgets_relay.reset()
    ha_events.reset()
    yield
    gadgets.reset()
    gadgets_buttons.reset()
    gadgets_relay.reset()
    ha_events.reset()


@pytest.fixture
def satellite(monkeypatch):
    """Put settings into relayed-satellite shape and keep the worker inert."""
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "relay_gadgets_upstream", True, raising=False)
    monkeypatch.setattr(settings, "remote_server_url", "http://server:9284",
                        raising=False)
    monkeypatch.setattr(settings, "upstream_api_key", "upkey", raising=False)
    monkeypatch.setattr(gadgets_relay, "_ensure_worker", lambda: None)
    monkeypatch.setattr(gadgets_relay, "relay_source", lambda: "bandit-autopi")


@pytest.fixture
def server_mode(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)


# -- Pure helpers ---------------------------------------------------------------

def test_tag_payload_stamps_source_and_drops_adapter_block():
    payload = {"devices": [{"id": "aa", "kind": "hygrometer", "temp_c": 4.0}],
               "discovered": [{"id": "bb", "kind": "contact"}],
               "bluetooth": {"available": False, "detail": "off"}}
    out = gadgets_relay.tag_payload(payload, "bandit")
    assert out["source"] == "bandit"
    assert "bluetooth" not in out
    assert out["devices"][0]["id"] == "aa"
    # Shallow copies: the queue never aliases the caller's dicts.
    out["devices"][0]["temp_c"] = 99.0
    assert payload["devices"][0]["temp_c"] == 4.0
    # No source means no tag key at all.
    assert "source" not in gadgets_relay.tag_payload(payload, "")


def test_merge_payloads_newest_reading_wins_and_events_survive():
    older = {"devices": [
        {"id": "AA", "kind": "hygrometer", "temp_c": 4.0},
        {"id": "BB", "kind": "button", "event": {"type": "single", "button": 1}},
    ], "discovered": [{"id": "CC", "kind": "contact", "rssi": -70}],
        "source": "bandit"}
    newer = {"devices": [
        {"id": "aa", "kind": "hygrometer", "temp_c": 4.5},
        {"id": "BB", "kind": "button", "event": {"type": "double", "button": 1}},
    ], "discovered": [{"id": "CC", "kind": "contact", "rssi": -60}]}
    merged = gadgets_relay.merge_payloads(older, newer)
    hygros = [d for d in merged["devices"] if d.get("kind") == "hygrometer"]
    assert len(hygros) == 1 and hygros[0]["temp_c"] == 4.5
    events = [d for d in merged["devices"] if d.get("event")]
    assert [e["event"]["type"] for e in events] == ["single", "double"]
    assert merged["discovered"] == [{"id": "CC", "kind": "contact", "rssi": -60}]
    assert merged["source"] == "bandit"


def test_merge_payloads_caps_hoarded_events(monkeypatch):
    monkeypatch.setattr(gadgets_relay, "MERGE_EVENT_MAX", 3)
    older = {"devices": [{"id": "BB", "kind": "button",
                          "event": {"type": "single", "counter": i}}
                         for i in range(5)]}
    merged = gadgets_relay.merge_payloads(older, {"devices": []})
    counters = [d["event"]["counter"] for d in merged["devices"]]
    assert counters == [2, 3, 4]  # newest kept


def test_merge_device_lists_unions_by_id_server_wins():
    local = [{"id": "aa:bb", "name": "Local name"}, {"id": "dd", "name": "Mine"}]
    upstream = [{"id": "AA:BB", "name": "Server name", "location": "Fridge"},
                {"id": "ee", "name": "Server only"},
                {"name": "no id, dropped"}, "junk"]
    merged = gadgets_relay.merge_device_lists(local, upstream)
    assert [d.get("name") for d in merged] == ["Server name", "Mine", "Server only"]
    assert merged[0]["location"] == "Fridge"
    assert gadgets_relay.merge_device_lists(None, None) == []


def test_normalize_upstream_gadget_config_keeps_only_known_shape():
    raw = {"gadgets_enabled": 1, "gadget_devices": [{"id": "aa"}, {"x": 1}, 3],
           "hygrometer_devices": "junk", "buttons_enabled": False,
           "cub_ble_advertise": True, "device_id": "srv-1",
           "unknown_key": "dropped"}
    out = gadgets_relay.normalize_upstream_gadget_config(raw)
    assert out == {"gadgets_enabled": True, "gadget_devices": [{"id": "aa"}],
                   "buttons_enabled": False, "cub_ble_advertise": True,
                   "device_id": "srv-1"}
    assert gadgets_relay.normalize_upstream_gadget_config("junk") == {}


# -- Activation gate --------------------------------------------------------------

def test_relay_active_needs_mode_toggle_and_link(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "relay_gadgets_upstream", True, raising=False)
    monkeypatch.setattr(settings, "remote_server_url", "http://s", raising=False)
    monkeypatch.setattr(settings, "upstream_api_key", "k", raising=False)
    assert gadgets_relay.relay_active() is True
    for field, value in (("deployment_mode", "server"),
                         ("relay_gadgets_upstream", False),
                         ("remote_server_url", ""),
                         ("upstream_api_key", "")):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(settings, field, value, raising=False)
            assert gadgets_relay.relay_active() is False


# -- Queue and drain ---------------------------------------------------------------

def test_enqueue_tags_and_bounds_the_queue(satellite, monkeypatch):
    monkeypatch.setattr(gadgets_relay, "QUEUE_MAX", 2)
    for i in range(4):
        assert gadgets_relay.enqueue(
            {"devices": [{"id": "AA", "kind": "hygrometer", "temp_c": float(i)}]})
    assert gadgets_relay.pending() == 2
    # Head is the coalesced older pushes, newest reading winning.
    head = gadgets_relay._queue[0]
    assert head["source"] == "bandit-autopi"
    assert [d["temp_c"] for d in head["devices"]] == [2.0]


def test_enqueue_skips_empty_pushes_and_inactive_relay(satellite):
    assert gadgets_relay.enqueue({"devices": [], "discovered": []}) is False
    assert gadgets_relay.pending() == 0


def test_drain_once_delivers_and_retries(satellite, monkeypatch):
    sent = []
    ok = {"value": False}
    monkeypatch.setattr(gadgets_relay, "_post",
                        lambda item: sent.append(item) or ok["value"])
    gadgets_relay.enqueue({"devices": [{"id": "AA", "kind": "hygrometer",
                                        "temp_c": 4.0}]})
    gadgets_relay.enqueue({"devices": [{"id": "AA", "kind": "hygrometer",
                                        "temp_c": 4.5}]})
    # Server down: the head merges into the next push and stays queued.
    assert gadgets_relay._drain_once() == "failed"
    assert gadgets_relay.pending() == 1
    # Server back: one combined delivery with the freshest reading.
    ok["value"] = True
    assert gadgets_relay._drain_once() == "sent"
    assert gadgets_relay.pending() == 0
    assert sent[-1]["devices"][0]["temp_c"] == 4.5
    assert gadgets_relay._drain_once() == "idle"


def test_drain_clears_queue_when_relay_turned_off(satellite, monkeypatch):
    gadgets_relay.enqueue({"devices": [{"id": "AA", "kind": "hygrometer",
                                        "temp_c": 4.0}]})
    monkeypatch.setattr(settings, "relay_gadgets_upstream", False, raising=False)
    assert gadgets_relay._drain_once() == "idle"
    assert gadgets_relay.pending() == 0


# -- Server side: source tagging and dedupe ------------------------------------------

def _relayed_push():
    return {"source": "bandit-autopi", "devices": [
        {"id": "AA:11", "kind": "hygrometer", "protocol": "govee_hygro",
         "name": "Fridge", "temp_c": 4.0, "humidity": 40.0},
        {"id": "BB:22", "kind": "contact", "protocol": "bthome_contact",
         "name": "Freezer door", "open": True},
        {"id": "CC:33", "protocol": "combustion", "name": "Probe",
         "probes": [{"index": 1, "temp_c": 55.0}]},
    ], "discovered": [
        {"id": "DD:44", "kind": "hygrometer", "protocol": "xiaomi_atc",
         "name": "Pantry", "rssi": -71},
    ]}


def test_server_ingest_records_via_and_relay_heartbeat(server_mode, monkeypatch):
    monkeypatch.setattr(settings, "hygrometer_devices",
                        [{"id": "AA:11", "name": "Fridge"}], raising=False)
    monkeypatch.setattr(settings, "contact_devices",
                        [{"id": "BB:22", "name": "Freezer door"}], raising=False)
    monkeypatch.setattr(settings, "gadget_devices",
                        [{"id": "CC:33", "name": "Probe"}], raising=False)
    result = asyncio.run(gadgets_router.post_readings(_relayed_push()))
    assert result["ok"] is True
    state = gadgets.get_state()
    assert state["hygrometers"][0]["via"] == "bandit-autopi"
    assert state["contacts"][0]["via"] == "bandit-autopi"
    assert state["devices"][0]["via"] == "bandit-autopi"
    assert state["hygro_discovered"][0]["via"] == "bandit-autopi"
    # A relayed push is not this install's own reader heartbeat, but the
    # relay heartbeat records that (and through whom) data arrives.
    assert state["reader_age_seconds"] is None
    assert state["relay_age_seconds"] is not None
    assert state["relay_source"] == "bandit-autopi"


def test_server_local_reading_after_relay_wins_and_clears_via(server_mode, monkeypatch):
    monkeypatch.setattr(settings, "hygrometer_devices",
                        [{"id": "AA:11", "name": "Fridge"}], raising=False)
    monkeypatch.setattr(settings, "contact_devices", [], raising=False)
    monkeypatch.setattr(settings, "gadget_devices", [], raising=False)
    gadgets.ingest({"source": "bandit-autopi", "devices": [
        {"id": "AA:11", "kind": "hygrometer", "temp_c": 4.0}]},
        mark_reader=False)
    # The same fridge sensor heard by the server's own radio a moment later.
    gadgets.ingest({"devices": [
        {"id": "AA:11", "kind": "hygrometer", "temp_c": 4.2}]})
    hygro = gadgets.get_state()["hygrometers"][0]
    assert hygro["temp_c"] == 4.2 and hygro["via"] == ""


def test_server_executes_relayed_button_press(server_mode, monkeypatch):
    monkeypatch.setattr(settings, "buttons_enabled", True, raising=False)
    monkeypatch.setattr(settings, "button_devices", [
        {"id": "EE:55", "name": "Shelf", "protocol": "bthome_button",
         "mappings": {"single": {"action": "shopping_add",
                                 "product_name": "Paper Towels"}}}],
        raising=False)
    ran = []

    async def fake_execute(dev, mapping):
        ran.append(mapping["product_name"])
        return True

    monkeypatch.setattr(gadgets_buttons, "_execute", fake_execute)
    push = {"source": "bandit-autopi", "devices": [
        {"id": "EE:55", "kind": "button", "protocol": "bthome_button",
         "battery": 90, "event": {"button": 1, "type": "single"}}]}
    asyncio.run(gadgets_router.post_readings(push))
    assert ran == ["Paper Towels"]
    snap = gadgets_buttons.state_snapshot()
    assert snap["buttons"][0]["via"] == "bandit-autopi"


# -- Satellite side: forward, defer alarms, do not double-execute --------------------

def test_satellite_forwards_push_and_skips_button_execution(satellite, monkeypatch):
    monkeypatch.setattr(settings, "buttons_enabled", True, raising=False)
    monkeypatch.setattr(settings, "button_devices", [
        {"id": "EE:55", "name": "Shelf", "protocol": "bthome_button",
         "mappings": {"single": {"action": "shopping_add",
                                 "product_name": "Paper Towels"}}}],
        raising=False)

    async def boom(dev, mapping):  # pragma: no cover - must not run
        raise AssertionError("a relaying satellite must not execute mappings")

    monkeypatch.setattr(gadgets_buttons, "_execute", boom)
    push = {"devices": [
        {"id": "EE:55", "kind": "button", "protocol": "bthome_button",
         "battery": 90, "event": {"button": 1, "type": "single"}}]}
    result = asyncio.run(gadgets_router.post_readings(push))
    assert result["button_events"] == 1
    # The press was queued for the server, tagged with this satellite's name.
    assert gadgets_relay.pending() == 1
    queued = gadgets_relay._queue[0]
    assert queued["source"] == "bandit-autopi"
    assert queued["devices"][0]["event"]["type"] == "single"
    # Local state still shows the press (battery, last seen, last event).
    last = gadgets_buttons.state_snapshot()["buttons"][0]["last_event"]
    assert last and last["type"] == "single"


def test_satellite_defers_protection_alarms_to_the_server(satellite, monkeypatch):
    monkeypatch.setattr(settings, "gadget_devices", [], raising=False)
    monkeypatch.setattr(settings, "hygrometers_enabled", True, raising=False)
    monkeypatch.setattr(settings, "hygrometer_devices", [
        {"id": "AA:11", "name": "Fridge",
         "thresholds": {"max_temp_c": 7.0}, "alarm_grace_seconds": 0}],
        raising=False)
    t0 = 1_700_000_000.0
    gadgets.ingest({"devices": [{"id": "AA:11", "kind": "hygrometer",
                                 "temp_c": 12.0}]}, now=t0)
    assert gadgets.run_protection_sweep(t0 + 3600) == []
    assert gadgets.active_alarms(t0 + 3600) == []
    # The same breach alarms as soon as the relay is off again.
    monkeypatch.setattr(settings, "relay_gadgets_upstream", False, raising=False)
    fired = gadgets.run_protection_sweep(t0 + 3600)
    assert [f["breach"]["kind"] for f in fired] == ["temp_high"]


def test_satellite_reader_config_merges_server_lists(satellite, monkeypatch):
    monkeypatch.setattr(settings, "gadgets_enabled", False, raising=False)
    monkeypatch.setattr(settings, "gadget_devices", [], raising=False)
    monkeypatch.setattr(settings, "hygrometers_enabled", True, raising=False)
    monkeypatch.setattr(settings, "hygrometer_devices",
                        [{"id": "AA:11", "name": "Local fridge"}], raising=False)
    monkeypatch.setattr(settings, "buttons_enabled", False, raising=False)
    monkeypatch.setattr(settings, "button_devices", [], raising=False)
    monkeypatch.setattr(settings, "contacts_enabled", False, raising=False)
    monkeypatch.setattr(settings, "contact_devices", [], raising=False)
    monkeypatch.setattr(settings, "cub_ble_advertise", False, raising=False)
    monkeypatch.setattr(settings, "device_id", "local-dev", raising=False)
    monkeypatch.setattr(settings, "upstream_gadget_config", {
        "gadgets_enabled": True,
        "gadget_devices": [{"id": "CC:33", "name": "Server probe"}],
        "hygrometers_enabled": True,
        "hygrometer_devices": [{"id": "AA:11", "name": "Kitchen fridge",
                                "thresholds": {"max_temp_c": 7.0}},
                               {"id": "FF:66", "name": "Server pantry"}],
        "buttons_enabled": True,
        "button_devices": [{"id": "EE:55", "name": "Server shelf"}],
        "cub_ble_advertise": True,
        "device_id": "server-dev",
    }, raising=False)
    cfg = asyncio.run(gadgets_router.reader_config())
    assert cfg["enabled"] is True
    assert [d["id"] for d in cfg["devices"]] == ["CC:33"]
    # The server's entry wins the shared id; local-only entries survive.
    assert [d["name"] for d in cfg["hygrometers"]] == ["Kitchen fridge",
                                                       "Server pantry"]
    assert cfg["buttons_enabled"] is True
    assert [d["id"] for d in cfg["buttons"]] == ["EE:55"]
    assert cfg["contacts_enabled"] is False and cfg["contacts"] == []
    # The Cub broadcast keeps carrying the SERVER's flag and install tag.
    assert cfg["cub_ble_advertise"] is True
    assert cfg["device_id"] == "server-dev"


def test_reader_config_unchanged_off_satellite(server_mode, monkeypatch):
    monkeypatch.setattr(settings, "gadgets_enabled", True, raising=False)
    monkeypatch.setattr(settings, "gadget_devices",
                        [{"id": "AA", "name": "Mine"}], raising=False)
    monkeypatch.setattr(settings, "upstream_gadget_config",
                        {"gadget_devices": [{"id": "ZZ"}]}, raising=False)
    cfg = asyncio.run(gadgets_router.reader_config())
    # A non-satellite never merges someone else's lists.
    assert [d["id"] for d in cfg["devices"]] == ["AA"]


# -- Satellite sync: the server's lists flow back down --------------------------------

def test_sync_mirrors_gadget_config_only_when_present(satellite, monkeypatch):
    saved = []
    monkeypatch.setattr(settings, "upstream_gadget_config", {}, raising=False)
    monkeypatch.setattr(settings.__class__, "save",
                        lambda self, data: saved.append(dict(data)))
    satellite_svc._apply_gadget_config({"config": {}})  # older server: no block
    assert saved == []
    block = {"gadget_config": {"hygrometers_enabled": True,
                               "hygrometer_devices": [{"id": "AA:11"}],
                               "junk": 1}}
    satellite_svc._apply_gadget_config(block)
    assert saved == [{"upstream_gadget_config": {
        "hygrometers_enabled": True,
        "hygrometer_devices": [{"id": "AA:11"}]}}]
    # Unchanged content is not persisted again.
    monkeypatch.setattr(settings, "upstream_gadget_config",
                        saved[0]["upstream_gadget_config"], raising=False)
    satellite_svc._apply_gadget_config(block)
    assert len(saved) == 1
