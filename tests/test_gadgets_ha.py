"""Home Assistant thermometer source (FoodAssistant-mnks).

Covers the pure entity parsing (unit conversion, unavailable states, battery,
stale last_updated), the poll gating, one mocked poll pass end to end into the
gadgets state, the picker filter, probe alerts (the integration's service
first, the older event route after a 400) and the daily token checks riding
the poll loop. No network or Home Assistant needed.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import gadgets, gadgets_ha  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    gadgets.reset()
    yield
    gadgets.reset()


NOW = 1_700_000_000.0


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


# -- entity_reading -----------------------------------------------------------

def test_entity_reading_celsius_passthrough():
    reading = gadgets_ha.entity_reading("sensor.grill", {
        "state": "74.5",
        "attributes": {"unit_of_measurement": "°C", "friendly_name": "Grill"},
        "last_updated": _iso(NOW - 5),
    }, NOW)
    assert reading == {
        "id": "HA:SENSOR.GRILL", "name": "Grill",
        "protocol": "home_assistant",
        "probes": [{"index": 1, "temp_c": 74.5}], "battery": None,
    }


def test_entity_reading_converts_fahrenheit():
    reading = gadgets_ha.entity_reading("sensor.grill", {
        "state": "212", "attributes": {"unit_of_measurement": "°F"},
        "last_updated": _iso(NOW),
    }, NOW)
    assert reading["probes"][0]["temp_c"] == 100.0


def test_entity_reading_battery_attribute_clamped():
    reading = gadgets_ha.entity_reading("sensor.grill", {
        "state": "20", "attributes": {"battery_level": 150},
    }, NOW)
    assert reading["battery"] == 100
    reading = gadgets_ha.entity_reading("sensor.grill", {
        "state": "20", "attributes": {"battery_level": "62"},
    }, NOW)
    assert reading["battery"] == 62


def test_entity_reading_skips_unavailable_and_junk():
    for state in ("unavailable", "unknown", "", None, "not-a-number"):
        assert gadgets_ha.entity_reading(
            "sensor.grill", {"state": state}, NOW) is None
    assert gadgets_ha.entity_reading("sensor.grill", "junk", NOW) is None


def test_entity_reading_skips_stale_last_updated():
    body = {"state": "50", "last_updated": _iso(NOW - 3600)}
    assert gadgets_ha.entity_reading("sensor.grill", body, NOW) is None
    # A missing or unparseable timestamp is not treated as stale.
    assert gadgets_ha.entity_reading("sensor.grill", {"state": "50"}, NOW)


def test_valid_entity_id():
    assert gadgets_ha.valid_entity_id("sensor.grill_probe_1")
    assert not gadgets_ha.valid_entity_id("grill")
    assert not gadgets_ha.valid_entity_id("sensor.grill probe")
    assert not gadgets_ha.valid_entity_id("")
    assert not gadgets_ha.valid_entity_id("sensor.grill.probe")


def test_has_current_value():
    assert gadgets_ha.has_current_value("72.5")
    assert gadgets_ha.has_current_value(72.5)
    assert gadgets_ha.has_current_value("0")
    assert not gadgets_ha.has_current_value("unavailable")
    assert not gadgets_ha.has_current_value("unknown")
    assert not gadgets_ha.has_current_value("")
    assert not gadgets_ha.has_current_value(None)
    assert not gadgets_ha.has_current_value("not-a-number")


def test_is_temperature_entity_filter():
    assert gadgets_ha.is_temperature_entity({
        "entity_id": "sensor.probe",
        "attributes": {"device_class": "temperature"}})
    assert gadgets_ha.is_temperature_entity({
        "entity_id": "sensor.probe",
        "attributes": {"unit_of_measurement": "°F"}})
    assert not gadgets_ha.is_temperature_entity({
        "entity_id": "binary_sensor.door",
        "attributes": {"device_class": "temperature"}})
    assert not gadgets_ha.is_temperature_entity({
        "entity_id": "sensor.humidity",
        "attributes": {"unit_of_measurement": "%"}})


# -- gating and polling -------------------------------------------------------

def _configure(monkeypatch, tmp_path, entities):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "gadget_ha_enabled", True, raising=False)
    monkeypatch.setattr(settings, "gadget_ha_entities", entities, raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_base_url",
                        "http://ha.local:8123", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "tok", raising=False)
    monkeypatch.setattr(settings, "gadgets_enabled", True, raising=False)
    monkeypatch.setattr(settings, "gadget_devices", [
        {"id": gadgets_ha.device_id_for(e), "name": e,
         "protocol": "home_assistant", "targets": {}} for e in entities
    ], raising=False)


def test_source_active_gating(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, ["sensor.grill"])
    assert gadgets_ha.source_active()
    monkeypatch.setattr(settings, "gadget_ha_enabled", False, raising=False)
    assert not gadgets_ha.source_active()
    monkeypatch.setattr(settings, "gadget_ha_enabled", True, raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "", raising=False)
    assert not gadgets_ha.source_active()
    monkeypatch.setattr(settings, "streamdeck_ha_token", "tok", raising=False)
    monkeypatch.setattr(settings, "gadget_ha_entities", [], raising=False)
    assert not gadgets_ha.source_active()


def test_configured_entities_sanitizes(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "gadget_ha_entities",
                        ["sensor.grill", "Sensor.Grill", "junk", "", None],
                        raising=False)
    assert gadgets_ha.configured_entities() == ["sensor.grill"]


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _FakeClient:
    """A stand-in for httpx.AsyncClient: entity url -> canned response."""

    def __init__(self, responses):
        self.responses = responses
        self.requested = []

    async def get(self, url, headers=None):
        self.requested.append((url, headers))
        for suffix, resp in self.responses.items():
            if url.endswith(suffix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return _FakeResponse(404, {})


def test_poll_once_ingests_readings_without_reader_heartbeat(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, ["sensor.grill", "sensor.oven"])
    client = _FakeClient({
        "/api/states/sensor.grill": _FakeResponse(200, {
            "state": "150.0",
            "attributes": {"unit_of_measurement": "°F",
                           "friendly_name": "Grill", "battery_level": 80},
            "last_updated": _iso(time.time()),
        }),
        # One dead entity must not starve the rest.
        "/api/states/sensor.oven": ConnectionError("down"),
    })
    count = asyncio.run(gadgets_ha.poll_once(client=client))
    assert count == 1
    # The bearer token went out on the request.
    assert client.requested[0][1] == {"Authorization": "Bearer tok"}

    state = gadgets.get_state()
    dev = {d["id"]: d for d in state["devices"]}["HA:SENSOR.GRILL"]
    assert dev["probes"][0]["temp_c"] == pytest.approx(65.56, abs=0.01)
    assert dev["battery"] == 80
    assert dev["stale"] is False
    # HA readings are not the Bluetooth reader: no reader heartbeat.
    assert state["reader_age_seconds"] is None


def test_poll_once_noop_when_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "", raising=False)
    monkeypatch.setattr(settings, "gadget_ha_entities", [], raising=False)
    assert asyncio.run(gadgets_ha.poll_once(client=_FakeClient({}))) == 0


def test_target_alert_fires_on_ha_reading(monkeypatch, tmp_path):
    """Alerts evaluate on ingest, so an HA-sourced reading crossing its target
    fires the same toast as a reader-sourced one."""
    from app.services import ha_events
    ha_events.reset()
    _configure(monkeypatch, tmp_path, ["sensor.grill"])
    # The outbound Home Assistant alert runs on a thread; keep it off the
    # network and just record that it was asked for.
    sent = []
    monkeypatch.setattr(gadgets_ha, "deliver_probe_alerts",
                        lambda base, token, payloads, client=None: sent.append(payloads))
    monkeypatch.setattr(settings, "gadget_devices", [
        {"id": "HA:SENSOR.GRILL", "name": "Grill",
         "protocol": "home_assistant",
         "targets": {"1": {"temp_c": 60.0, "direction": "above"}}},
    ], raising=False)
    client = _FakeClient({
        "/api/states/sensor.grill": _FakeResponse(200, {
            "state": "74", "attributes": {"unit_of_measurement": "°C"},
            "last_updated": _iso(time.time()),
        }),
    })
    asyncio.run(gadgets_ha.poll_once(client=client))
    events = ha_events.poll(0)["events"]
    assert any("target" in e.get("message", "")
               and e.get("title") == "Grill" for e in events)
    ha_events.reset()


def test_list_temperature_entities_filters_and_sorts(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, ["sensor.grill"])
    client = _FakeClient({
        "/api/states": _FakeResponse(200, [
            {"entity_id": "sensor.zeta_probe", "state": "50",
             "attributes": {"device_class": "temperature",
                            "friendly_name": "Zeta"}},
            {"entity_id": "sensor.alpha_probe", "state": "60",
             "attributes": {"unit_of_measurement": "°F",
                            "friendly_name": "Alpha"}},
            {"entity_id": "sensor.humidity", "state": "40",
             "attributes": {"unit_of_measurement": "%"}},
            {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
            {"entity_id": "sensor.dead_probe", "state": "unavailable",
             "attributes": {"device_class": "temperature",
                            "friendly_name": "Dead"}},
        ]),
    })
    rows = asyncio.run(gadgets_ha.list_temperature_entities(client=client))
    assert [r["entity_id"] for r in rows] == [
        "sensor.alpha_probe", "sensor.dead_probe", "sensor.zeta_probe"]
    assert rows[0]["unit"] == "°F"
    by_id = {r["entity_id"]: r for r in rows}
    assert by_id["sensor.alpha_probe"]["has_value"] is True
    assert by_id["sensor.dead_probe"]["has_value"] is False


def test_list_temperature_entities_empty_when_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "", raising=False)
    assert asyncio.run(gadgets_ha.list_temperature_entities(
        client=_FakeClient({}))) == []


# -- group_entities_into_devices (FoodAssistant-ck77) -------------------------

def test_group_entities_by_shared_device_id():
    entities = [
        {"entity_id": "sensor.grill_1", "name": "Grill Probe 1",
         "device_id": "abc123"},
        {"entity_id": "sensor.grill_2", "name": "Grill Probe 2",
         "device_id": "abc123"},
        {"entity_id": "sensor.fridge", "name": "Fridge", "device_id": "xyz789"},
    ]
    groups = gadgets_ha.group_entities_into_devices(entities)
    assert groups == [
        {"device_name": "Grill", "entity_ids": ["sensor.grill_1", "sensor.grill_2"]},
    ]


def test_group_entities_by_probe_name_prefix():
    entities = [
        {"entity_id": "sensor.grill_probe_1", "name": "Grill Probe 1"},
        {"entity_id": "sensor.grill_probe_2", "name": "Grill Probe 2"},
        {"entity_id": "sensor.smoker_probe_1", "name": "Smoker Probe 1"},
        {"entity_id": "sensor.smoker_probe_2", "name": "Smoker Probe 2"},
    ]
    groups = gadgets_ha.group_entities_into_devices(entities)
    assert groups == [
        {"device_name": "Grill",
         "entity_ids": ["sensor.grill_probe_1", "sensor.grill_probe_2"]},
        {"device_name": "Smoker",
         "entity_ids": ["sensor.smoker_probe_1", "sensor.smoker_probe_2"]},
    ]


def test_group_entities_bare_number_suffix():
    entities = [
        {"entity_id": "sensor.grill_1", "name": "Grill 1"},
        {"entity_id": "sensor.grill_2", "name": "Grill 2"},
    ]
    groups = gadgets_ha.group_entities_into_devices(entities)
    assert groups == [
        {"device_name": "Grill", "entity_ids": ["sensor.grill_1", "sensor.grill_2"]},
    ]


def test_group_entities_unrelated_singletons_stay_separate():
    entities = [
        {"entity_id": "sensor.fridge", "name": "Fridge"},
        {"entity_id": "sensor.freezer", "name": "Freezer"},
        {"entity_id": "sensor.wine_cooler", "name": "Wine Cooler"},
    ]
    assert gadgets_ha.group_entities_into_devices(entities) == []


def test_group_entities_ignores_junk_rows():
    entities = [
        "not-a-dict",
        {"name": "no entity id"},
        {"entity_id": "sensor.grill_probe_1", "name": "Grill Probe 1"},
        {"entity_id": "sensor.grill_probe_2", "name": "Grill Probe 2"},
    ]
    groups = gadgets_ha.group_entities_into_devices(entities)
    assert len(groups) == 1
    assert groups[0]["entity_ids"] == ["sensor.grill_probe_1", "sensor.grill_probe_2"]


# -- probe alerts to Home Assistant ---------------------------------------------

class _SyncResp:
    def __init__(self, status_code):
        self.status_code = status_code


class _SyncClient:
    """A stand-in for httpx.Client: path suffix -> status (or an error)."""

    def __init__(self, statuses):
        self.statuses = statuses
        self.posts = []

    def post(self, url, headers=None, json=None):
        self.posts.append((url, headers, json))
        for suffix, status in self.statuses.items():
            if url.endswith(suffix):
                if isinstance(status, Exception):
                    raise status
                return _SyncResp(status)
        return _SyncResp(404)

    def close(self):
        pass


_SERVICE = "/api/services/pantry_raider/probe_alert"
_EVENT = "/api/events/pantry_raider_probe_alert"
_PAYLOAD = {"device_id": "HA:SENSOR.GRILL", "probe": "1", "message": "Grill is done"}


def _probe_status(monkeypatch, tmp_path):
    from app.services import ha_client
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    return ha_client


def test_probe_alert_outcome():
    from app.services.ha_client import PROBE_ALERT_UPDATE_MSG, UNREACHABLE_MSG
    assert gadgets_ha.probe_alert_outcome(200, None) == (True, "")
    assert gadgets_ha.probe_alert_outcome(400, 200) == (True, "")
    assert gadgets_ha.probe_alert_outcome(400, 401) == (False, PROBE_ALERT_UPDATE_MSG)
    assert gadgets_ha.probe_alert_outcome(400, 0) == (False, PROBE_ALERT_UPDATE_MSG)
    assert gadgets_ha.probe_alert_outcome(0, None) == (False, UNREACHABLE_MSG)
    assert gadgets_ha.probe_alert_outcome(500, None) == (False, "Home Assistant answered 500.")


def test_probe_alert_uses_the_integration_service_first(monkeypatch, tmp_path):
    ha_client = _probe_status(monkeypatch, tmp_path)
    client = _SyncClient({_SERVICE: 200})
    ok, detail = gadgets_ha.deliver_probe_alerts("http://ha.test:8123", "tok",
                                                 [_PAYLOAD], client=client)
    assert ok is True and detail == ""
    assert [p[0] for p in client.posts] == ["http://ha.test:8123" + _SERVICE]
    assert client.posts[0][1] == {"Authorization": "Bearer tok"}
    assert client.posts[0][2] == _PAYLOAD
    assert ha_client.status_snapshot()["probe_alert"]["ok"] is True


def test_probe_alert_falls_back_to_the_event_on_400(monkeypatch, tmp_path):
    ha_client = _probe_status(monkeypatch, tmp_path)
    client = _SyncClient({_SERVICE: 400, _EVENT: 200})
    ok, _detail = gadgets_ha.deliver_probe_alerts("http://ha.test:8123", "tok",
                                                  [_PAYLOAD], client=client)
    assert ok is True
    assert [p[0] for p in client.posts] == ["http://ha.test:8123" + _SERVICE,
                                            "http://ha.test:8123" + _EVENT]
    assert client.posts[1][2] == _PAYLOAD
    assert ha_client.status_snapshot()["probe_alert"]["ok"] is True


def test_probe_alert_records_when_both_fail(monkeypatch, tmp_path):
    ha_client = _probe_status(monkeypatch, tmp_path)
    client = _SyncClient({_SERVICE: 400, _EVENT: 401})
    ok, detail = gadgets_ha.deliver_probe_alerts("http://ha.test:8123", "tok",
                                                 [_PAYLOAD], client=client)
    assert ok is False
    assert detail == "Probe alerts need the updated Pantry Raider integration in Home Assistant."
    snap = ha_client.status_snapshot()["probe_alert"]
    assert snap["ok"] is False and snap["detail"] == detail


@pytest.mark.parametrize("kind,admin", [("dedicated", None), ("manual", False)])
def test_probe_alert_skips_the_event_route_for_a_limited_token(monkeypatch, tmp_path,
                                                                kind, admin):
    """The event route is admin-only, and Home Assistant counts a limited
    account's 401 there as a failed sign-in (a notification, and a ban where
    a sign-in limit is set), so it is never tried with such a token."""
    _probe_status(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "ha_token_kind", kind, raising=False)
    monkeypatch.setattr(settings, "ha_token_admin", admin, raising=False)
    client = _SyncClient({_SERVICE: 400, _EVENT: 200})
    ok, detail = gadgets_ha.deliver_probe_alerts("http://ha.test:8123", "tok",
                                                 [_PAYLOAD], client=client)
    assert ok is False
    assert detail == "Probe alerts need the updated Pantry Raider integration in Home Assistant."
    assert [p[0] for p in client.posts] == ["http://ha.test:8123" + _SERVICE]


def test_probe_alert_no_fallback_on_other_failures(monkeypatch, tmp_path):
    _probe_status(monkeypatch, tmp_path)
    client = _SyncClient({_SERVICE: ConnectionError("down")})
    ok, _detail = gadgets_ha.deliver_probe_alerts("http://ha.test:8123", "tok",
                                                  [_PAYLOAD], client=client)
    assert ok is False
    assert len(client.posts) == 1


def test_notify_probe_alerts_hands_payloads_to_a_thread(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, ["sensor.grill"])
    seen = []
    monkeypatch.setattr(gadgets_ha, "deliver_probe_alerts",
                        lambda base, token, payloads, client=None: seen.append((base, token, payloads)))

    class _NowThread:
        def __init__(self, target, args=(), daemon=None):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

    import threading
    monkeypatch.setattr(threading, "Thread", _NowThread)
    fired = [{"key": "HA:SENSOR.GRILL:1", "temp_c": 74.0, "target_c": 70.0,
              "direction": "above"}]
    gadgets_ha.notify_probe_alerts(fired, [{"id": "HA:SENSOR.GRILL", "name": "Grill"}], "c")
    assert len(seen) == 1
    base, token, payloads = seen[0]
    assert (base, token) == ("http://ha.local:8123", "tok")
    assert payloads[0]["device_name"] == "Grill"


def test_poll_loop_runs_the_daily_checks(monkeypatch, tmp_path):
    from app.services import ha_client
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(gadgets_ha, "source_active", lambda: False)
    calls = []

    async def fake_checks(now=None):
        calls.append(now)

    async def stop(_seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(ha_client, "maybe_run_daily_checks", fake_checks)
    monkeypatch.setattr(gadgets_ha.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(gadgets_ha.poll_loop())
    assert calls == [None]
