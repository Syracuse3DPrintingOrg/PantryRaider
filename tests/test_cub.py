"""Bandit Cub tests (FoodAssistant-bzqj / FoodAssistant-mxof).

Covers the pure summary builder in services/cub.py (view-decision priority,
settings merge, rotation filtering, block assembly, degradation), the Cub
device registry (heartbeat upsert from headers, online window, rename,
overrides, delete), the GET /cub/summary contract against the design doc's
example shape, and the Bandit Cubs section of the Devices pane. No network,
Docker, or hardware: Grocy, timers, and gadgets are all monkeypatched, and
the registry runs on an isolated in-memory DB (the test_devices.py harness).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import cub as cub_svc  # noqa: E402

# Isolated in-memory DB so registry tests never touch the real data directory.
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from app.database import Base  # noqa: E402
from app.models import db_models  # noqa: F401,E402 - registers models with Base

# StaticPool: TestClient serves requests on another thread, and a plain
# :memory: engine would hand that thread its own fresh (empty) database.
_test_engine = create_engine("sqlite:///:memory:",
                             connect_args={"check_same_thread": False},
                             poolclass=StaticPool)
_TestSession = sessionmaker(bind=_test_engine)
Base.metadata.create_all(bind=_test_engine)


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch):
    monkeypatch.setattr(cub_svc, "SessionLocal", _TestSession)
    db = _TestSession()
    from app.models.db_models import CubDevice
    db.query(CubDevice).delete()
    db.commit()
    db.close()


def _defaults(**over) -> dict:
    base = {
        "default_view": "expiring",
        "timers_take_over": True,
        "probes_take_over": True,
        "rotation": ["expiring", "pending"],
        "rotate_seconds": 12,
        "poll_seconds": 15,
    }
    base.update(over)
    return base


_TIMER = {"id": 1, "label": "Pasta", "deadline_epoch": 1799990600.0, "expired": False}
_RINGING = {"id": 2, "label": "Eggs", "deadline_epoch": 1799989900.0, "expired": True}
_ARMED_PROBE = {"id": "IBT-4XS:AA11", "name": "Grill", "probe": 1,
                "temp_c": 63.5, "target_c": 74.0, "direction": "above", "stale": False}
_IDLE_PROBE = {"id": "IBT-4XS:AA11", "name": "Grill", "probe": 2,
               "temp_c": 21.0, "target_c": None, "direction": "above", "stale": False}


# ---------------------------------------------------------------------------
# decide_view: the doc's priority matrix
# ---------------------------------------------------------------------------

def test_view_timer_running_takes_over():
    assert cub_svc.decide_view([_TIMER], [], _defaults()) == "timers"


def test_view_timer_ringing_takes_over():
    # A just-expired timer is an alert waiting to be dismissed: still a takeover.
    assert cub_svc.decide_view([_RINGING], [], _defaults()) == "timers"


def test_view_timer_takeover_off_falls_through():
    merged = _defaults(timers_take_over=False)
    assert cub_svc.decide_view([_TIMER], [], merged) == "expiring"


def test_view_timers_beat_probes():
    assert cub_svc.decide_view([_TIMER], [_ARMED_PROBE], _defaults()) == "timers"


def test_view_armed_probe_takes_over_when_no_timer():
    assert cub_svc.decide_view([], [_ARMED_PROBE], _defaults()) == "probe"


def test_view_probe_without_target_does_not_take_over():
    assert cub_svc.decide_view([], [_IDLE_PROBE], _defaults()) == "expiring"


def test_view_probe_takeover_off_falls_through():
    merged = _defaults(probes_take_over=False)
    assert cub_svc.decide_view([], [_ARMED_PROBE], merged) == "expiring"


@pytest.mark.parametrize("default", ["expiring", "rotation", "clock"])
def test_view_idle_uses_configured_default(default):
    merged = _defaults(default_view=default)
    assert cub_svc.decide_view([], [], merged) == default


def test_view_unknown_default_falls_back_to_expiring():
    merged = _defaults(default_view="bogus")
    assert cub_svc.decide_view([], [], merged) == "expiring"


# ---------------------------------------------------------------------------
# merge_cub_settings
# ---------------------------------------------------------------------------

def test_merge_no_overrides_returns_global():
    assert cub_svc.merge_cub_settings(_defaults(), {}) == _defaults()


def test_merge_override_wins():
    merged = cub_svc.merge_cub_settings(_defaults(), {"default_view": "clock",
                                                      "rotate_seconds": 30})
    assert merged["default_view"] == "clock"
    assert merged["rotate_seconds"] == 30
    assert merged["poll_seconds"] == 15  # untouched


def test_merge_malformed_overrides_ignored():
    base = _defaults()
    # Not a dict at all.
    assert cub_svc.merge_cub_settings(base, None) == base
    assert cub_svc.merge_cub_settings(base, "junk") == base
    # Unknown keys, wrong types, and an unknown view are dropped, never applied.
    merged = cub_svc.merge_cub_settings(base, {
        "default_view": "bogus",
        "timers_take_over": "yes please",
        "rotate_seconds": "soon",
        "poll_seconds": True,     # bool is not an int here
        "not_a_setting": 42,
    })
    assert merged == base


def test_merge_never_mutates_global():
    base = _defaults()
    cub_svc.merge_cub_settings(base, {"default_view": "clock"})
    assert base["default_view"] == "expiring"


# ---------------------------------------------------------------------------
# rotation_blocks
# ---------------------------------------------------------------------------

def test_rotation_filters_unknown_blocks_and_dedupes():
    merged = _defaults(rotation=["pending", "expiring", "pending", "bogus", "clock"])
    assert cub_svc.rotation_blocks(merged) == ["pending", "expiring", "clock"]


def test_rotation_non_list_is_empty():
    assert cub_svc.rotation_blocks(_defaults(rotation="expiring")) == []


# ---------------------------------------------------------------------------
# expiring_block
# ---------------------------------------------------------------------------

def _item(name: str, days: int) -> dict:
    return {"product": {"name": name}, "days_remaining": days}


def test_expiring_block_counts_and_top():
    items = [_item("Milk", -1), _item("Bread", 0), _item("Chicken thighs", 2),
             _item("Spinach", 3), _item("Yogurt", 5), _item("Ketchup", 20)]
    block = cub_svc.expiring_block(items, 5)
    assert block["ok"] is True
    assert block["expired"] == 1
    assert block["today"] == 1
    assert block["soon"] == 3
    assert block["window_days"] == 5
    # Top 3 soonest, expired included, items beyond the window excluded.
    assert block["top"] == [{"name": "Milk", "days": -1},
                            {"name": "Bread", "days": 0},
                            {"name": "Chicken thighs", "days": 2}]


def test_expiring_block_degrades_to_zeros():
    for block in (cub_svc.expiring_block(None, 5),
                  cub_svc.expiring_block([_item("Milk", -1)], 5, ok=False)):
        assert block == {"ok": False, "expired": 0, "today": 0, "soon": 0,
                         "window_days": 5, "top": []}


# ---------------------------------------------------------------------------
# probes_block / timers_block
# ---------------------------------------------------------------------------

def test_probes_block_flattens_devices():
    devices = [{
        "id": "IBT-4XS:AA11", "name": "Grill", "stale": False,
        "probes": [
            {"index": 1, "temp_c": 63.5, "target_c": 74.0, "direction": "above"},
            {"index": 2, "temp_c": 21.0, "target_c": None, "direction": "above"},
        ],
    }]
    probes = cub_svc.probes_block(devices)
    assert probes == [
        {"id": "IBT-4XS:AA11", "name": "Grill", "probe": 1, "temp_c": 63.5,
         "target_c": 74.0, "direction": "above", "stale": False},
        {"id": "IBT-4XS:AA11", "name": "Grill", "probe": 2, "temp_c": 21.0,
         "target_c": None, "direction": "above", "stale": False},
    ]


def test_timers_block_shape():
    raw = [{"id": 3, "label": "Pasta", "deadline_epoch": 123.0, "expired": False,
            "remaining_seconds": 60, "running": True}]
    assert cub_svc.timers_block(raw) == [
        {"id": 3, "label": "Pasta", "deadline_epoch": 123, "expired": False}]


def test_timers_block_sends_a_whole_second_deadline():
    """Timers hold a float deadline internally (time.time() plus the duration).
    A firmware JSON reader that asks for an integer gets its default back from a
    value with a decimal point, so a float deadline read as 0 on the Cub and the
    screen sat at a stuck 0:00 (FoodAssistant-8qtx). The wire carries whole
    seconds; this test exists because the original one pinned the float and let
    the bug ship.
    """
    out = cub_svc.timers_block(
        [{"id": 1, "label": "Eggs", "deadline_epoch": 1784164563.3582869,
          "expired": False}])
    deadline = out[0]["deadline_epoch"]
    assert isinstance(deadline, int) and not isinstance(deadline, bool)
    assert deadline == 1784164563


def test_timers_block_survives_a_missing_or_odd_deadline():
    out = cub_svc.timers_block([
        {"id": 1, "label": "No deadline", "expired": False},
        {"id": 2, "label": "Null", "deadline_epoch": None, "expired": False},
        {"id": 3, "label": "Text", "deadline_epoch": "soon", "expired": False},
    ])
    assert [t["deadline_epoch"] for t in out] == [0, 0, 0]
    assert all(isinstance(t["deadline_epoch"], int) for t in out)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

def test_heartbeat_creates_and_updates_cub():
    over = cub_svc.record_cub_heartbeat(
        "cub-a4cf12", name="Stove shelf", hardware_profile="tdisplay",
        firmware_version="0.18.21", ip="10.0.0.9")
    assert over == {}
    rows = cub_svc.list_cubs()
    assert len(rows) == 1
    c = rows[0]
    assert c["device_id"] == "cub-a4cf12"
    assert c["name"] == "Stove shelf"
    assert c["hardware_profile"] == "tdisplay"
    assert c["firmware_version"] == "0.18.21"
    assert c["ip"] == "10.0.0.9"
    assert c["online"] is True

    cub_svc.record_cub_heartbeat("cub-a4cf12", firmware_version="0.19.0", ip="10.0.0.10")
    rows = cub_svc.list_cubs()
    assert len(rows) == 1
    assert rows[0]["firmware_version"] == "0.19.0"
    assert rows[0]["ip"] == "10.0.0.10"


def test_heartbeat_empty_id_is_a_noop():
    assert cub_svc.record_cub_heartbeat("") == {}
    assert cub_svc.list_cubs() == []


def test_heartbeat_name_never_overwrites_rename():
    cub_svc.record_cub_heartbeat("cub-1", name="cub-1")
    assert cub_svc.rename_cub("cub-1", "Fridge door") is True
    cub_svc.record_cub_heartbeat("cub-1", name="cub-1")
    assert cub_svc.list_cubs()[0]["name"] == "Fridge door"


def test_online_window():
    cub_svc.record_cub_heartbeat("cub-old")
    # Age the row past the window.
    db = _TestSession()
    from app.models.db_models import CubDevice
    dev = db.query(CubDevice).filter_by(device_id="cub-old").first()
    stale = datetime.now(timezone.utc) - timedelta(
        seconds=cub_svc.CUB_ONLINE_WINDOW_SECONDS + 60)
    dev.last_seen = stale.isoformat(timespec="seconds")
    db.commit()
    db.close()
    assert cub_svc.list_cubs()[0]["online"] is False


def test_overrides_roundtrip_and_heartbeat_returns_them():
    cub_svc.record_cub_heartbeat("cub-2")
    assert cub_svc.set_cub_overrides("cub-2", {"default_view": "clock",
                                               "junk_key": 1}) is True
    # Unknown keys are dropped on write; the poll hands the overrides back.
    assert cub_svc.record_cub_heartbeat("cub-2") == {"default_view": "clock"}
    assert cub_svc.list_cubs()[0]["overrides"] == {"default_view": "clock"}
    # An empty dict clears them.
    assert cub_svc.set_cub_overrides("cub-2", {}) is True
    assert cub_svc.record_cub_heartbeat("cub-2") == {}


def test_rename_and_delete_unknown_device():
    assert cub_svc.rename_cub("cub-nope", "x") is False
    assert cub_svc.set_cub_overrides("cub-nope", {}) is False
    assert cub_svc.forget_cub("cub-nope") is False


def test_forget_cub():
    cub_svc.record_cub_heartbeat("cub-3")
    assert cub_svc.forget_cub("cub-3") is True
    assert cub_svc.list_cubs() == []


# ---------------------------------------------------------------------------
# GET /cub/summary and the registry routes, over the app
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "api_key", "", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", [], raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr("app.hardware.is_raspberry_pi", lambda: False)
    from app.routers import expiring as expiring_router
    expiring_router._count_items_cache.invalidate()
    with TestClient(app) as c:
        yield c
    expiring_router._count_items_cache.invalidate()


def _stub_backends(monkeypatch, *, items=None, timers=None, gadget_devices=None,
                   grocy_down=False):
    from app.services.grocy import GrocyClient

    async def fake_get_expiring(self, days=7):
        if grocy_down:
            raise RuntimeError("grocy unreachable")
        return items or []
    monkeypatch.setattr(GrocyClient, "get_expiring", fake_get_expiring)

    from app.services import timers as timers_svc
    monkeypatch.setattr(timers_svc, "list_timers", lambda: list(timers or []))

    from app.services import gadgets
    monkeypatch.setattr(gadgets, "get_state",
                        lambda: {"devices": list(gadget_devices or [])})

    from app.routers import ha as ha_router
    monkeypatch.setattr(ha_router, "_counts_block",
                        lambda: {"pending": 2, "action_items": 1})


def test_summary_shape_matches_contract(client, monkeypatch):
    _stub_backends(
        monkeypatch,
        items=[_item("Milk", -1), _item("Chicken thighs", 2), _item("Spinach", 3)],
        timers=[{"id": 7, "label": "Pasta", "deadline_epoch": 1799990600.0,
                 "expired": False, "remaining_seconds": 600, "running": True}],
        gadget_devices=[{"id": "IBT-4XS:AA11", "name": "Grill", "stale": False,
                         "probes": [{"index": 1, "temp_c": 63.5, "target_c": 74.0,
                                     "direction": "above"}]}],
    )
    monkeypatch.setattr(settings, "expiring_soon_days", 5, raising=False)
    monkeypatch.setattr(settings, "streamdeck_weather_units", "f", raising=False)
    monkeypatch.setattr(settings, "clock_format", "auto", raising=False)

    body = client.get("/cub/summary").json()
    assert body["v"] == 1
    assert isinstance(body["generated"], int)
    assert body["view"] == "timers"  # a running timer wins
    assert body["rotation"] == ["expiring", "pending"]
    assert body["expiring"] == {"ok": True, "expired": 1, "today": 0, "soon": 2,
                                "window_days": 5,
                                "top": [{"name": "Milk", "days": -1},
                                        {"name": "Chicken thighs", "days": 2},
                                        {"name": "Spinach", "days": 3}]}
    assert body["counts"] == {"pending": 2, "action_items": 1}
    assert body["timers"] == [{"id": 7, "label": "Pasta",
                               "deadline_epoch": 1799990600.0, "expired": False}]
    assert body["probes"] == [{"id": "IBT-4XS:AA11", "name": "Grill", "probe": 1,
                               "temp_c": 63.5, "target_c": 74.0,
                               "direction": "above", "stale": False}]
    # No protection alarm is live, so the alerts block is a calm empty
    # list (FoodAssistant-5c61).
    assert body["alerts"] == []
    assert body["settings"] == {"default_view": "expiring",
                                "timers_take_over": True,
                                "probes_take_over": True,
                                "alerts_take_over": True,
                                "rotate_seconds": 12, "poll_seconds": 15,
                                "auto_update": True,
                                "units": "f", "clock_24h": False}


def test_summary_probe_takeover_when_idle(client, monkeypatch):
    _stub_backends(monkeypatch, gadget_devices=[
        {"id": "IBT-4XS:AA11", "name": "Grill", "stale": False,
         "probes": [{"index": 1, "temp_c": 63.5, "target_c": 74.0,
                     "direction": "above"}]}])
    body = client.get("/cub/summary").json()
    assert body["view"] == "probe"


def test_summary_degrades_when_grocy_down(client, monkeypatch):
    _stub_backends(monkeypatch, grocy_down=True)
    resp = client.get("/cub/summary")
    assert resp.status_code == 200  # never a 500
    body = resp.json()
    assert body["expiring"]["ok"] is False
    assert body["expiring"]["expired"] == 0
    assert body["view"] == "expiring"


def test_summary_headers_upsert_registry_row(client, monkeypatch):
    _stub_backends(monkeypatch)
    resp = client.get("/cub/summary", headers={
        "X-Cub-Id": "cub-a4cf12", "X-Cub-Profile": "touch7",
        "X-Cub-Version": "0.18.21", "X-Cub-Name": "Stove shelf"})
    assert resp.status_code == 200
    rows = cub_svc.list_cubs()
    assert len(rows) == 1
    assert rows[0]["device_id"] == "cub-a4cf12"
    assert rows[0]["hardware_profile"] == "touch7"
    assert rows[0]["firmware_version"] == "0.18.21"
    assert rows[0]["name"] == "Stove shelf"


def test_summary_without_headers_registers_nothing(client, monkeypatch):
    _stub_backends(monkeypatch)
    assert client.get("/cub/summary").status_code == 200
    assert cub_svc.list_cubs() == []


def test_summary_applies_per_cub_override(client, monkeypatch):
    _stub_backends(monkeypatch)
    cub_svc.record_cub_heartbeat("cub-42")
    cub_svc.set_cub_overrides("cub-42", {"default_view": "clock"})
    body = client.get("/cub/summary", headers={"X-Cub-Id": "cub-42"}).json()
    assert body["view"] == "clock"
    assert body["settings"]["default_view"] == "clock"
    # Another Cub (and a header-less poll) still gets the global default.
    assert client.get("/cub/summary").json()["view"] == "expiring"


def test_summary_auth_rides_api_key_middleware(client, monkeypatch):
    _stub_backends(monkeypatch)
    monkeypatch.setattr(settings, "auth_password", "hunter2", raising=False)
    monkeypatch.setattr(settings, "api_key", "test-key", raising=False)
    assert client.get("/cub/summary").status_code == 401
    assert client.get("/cub/summary",
                      headers={"X-API-Key": "test-key"}).status_code == 200


# ---------------------------------------------------------------------------
# registry management routes
# ---------------------------------------------------------------------------

def test_devices_routes_list_edit_delete(client, monkeypatch):
    _stub_backends(monkeypatch)
    cub_svc.record_cub_heartbeat("cub-9", hardware_profile="tdisplay")

    listed = client.get("/cub/devices").json()["devices"]
    assert [d["device_id"] for d in listed] == ["cub-9"]

    r = client.post("/cub/devices/cub-9",
                    json={"name": "Counter", "overrides": {"default_view": "clock"}})
    assert r.json() == {"ok": True}
    row = client.get("/cub/devices").json()["devices"][0]
    assert row["name"] == "Counter"
    assert row["overrides"] == {"default_view": "clock"}

    # A rename-only edit leaves the overrides alone.
    client.post("/cub/devices/cub-9", json={"name": "Counter 2"})
    row = client.get("/cub/devices").json()["devices"][0]
    assert row["name"] == "Counter 2"
    assert row["overrides"] == {"default_view": "clock"}

    assert client.post("/cub/devices/cub-nope", json={"name": "x"}).json()["ok"] is False

    assert client.delete("/cub/devices/cub-9").json() == {"ok": True}
    assert client.get("/cub/devices").json()["devices"] == []
    assert client.delete("/cub/devices/cub-9").json()["ok"] is False


# ---------------------------------------------------------------------------
# settings plumbing and the Devices pane
# ---------------------------------------------------------------------------

def test_cub_settings_are_saveable():
    from app.config import _SAVEABLE
    for f in ("cub_default_view", "cub_timers_take_over", "cub_probes_take_over",
              "cub_rotation", "cub_rotate_seconds", "cub_poll_seconds"):
        assert f in _SAVEABLE, f


def test_setup_save_validates_cub_fields(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    # Touch the fields through monkeypatch first so the save below is undone
    # at teardown and later tests still see the defaults.
    monkeypatch.setattr(settings, "cub_default_view", "expiring", raising=False)
    monkeypatch.setattr(settings, "cub_rotation", ["expiring", "pending"], raising=False)
    monkeypatch.setattr(settings, "cub_rotate_seconds", 12, raising=False)
    monkeypatch.setattr(settings, "cub_poll_seconds", 15, raising=False)
    r = client.post("/setup/save", json={
        "cub_default_view": "bogus",
        "cub_rotation": ["expiring", "bogus", "pending"],
        "cub_rotate_seconds": 0,
        "cub_poll_seconds": 9999,
    })
    assert r.json()["ok"] is True
    assert settings.cub_default_view == "expiring"     # unknown view falls back
    assert settings.cub_rotation == ["expiring", "pending"]  # unknown block dropped
    assert settings.cub_rotate_seconds == 3            # clamped to range
    assert settings.cub_poll_seconds == 300


def test_devices_pane_renders_cub_section(client, monkeypatch):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    try:
        with patch.object(type(settings), "is_configured", lambda self: True), \
             patch("app.routers.setup.is_raspberry_pi", return_value=False), \
             patch("app.templating.is_raspberry_pi", return_value=False):
            r = client.get("/setup")
    finally:
        os.chdir(cwd)
    assert r.status_code == 200
    assert "Bandit Cubs" in r.text
    for control in ("cub_default_view", "cub_timers_take_over",
                    "cub_probes_take_over", "cub_rotate_seconds",
                    "cub_poll_seconds", "cubs-list"):
        assert f'id="{control}"' in r.text, control


# ---------------------------------------------------------------------------
# Automatic firmware updates (FoodAssistant-abm5)
# ---------------------------------------------------------------------------

def test_auto_update_defaults_on():
    assert settings.model_fields["cub_auto_update"].default is True
    assert cub_svc.settings_block(_defaults(), units="f",
                                  clock_24h=False)["auto_update"] is True


def test_auto_update_comes_from_the_server_setting(monkeypatch):
    monkeypatch.setattr(settings, "cub_auto_update", False, raising=False)
    glob = cub_svc.global_cub_settings(settings)
    assert glob["auto_update"] is False
    assert cub_svc.settings_block(glob, units="f",
                                  clock_24h=False)["auto_update"] is False


def test_auto_update_per_cub_override_merges():
    glob = _defaults(auto_update=True)
    # One Cub opted out; the rest of the settings are untouched.
    merged = cub_svc.merge_cub_settings(glob, {"auto_update": False})
    assert merged["auto_update"] is False
    assert merged["default_view"] == "expiring"
    assert glob["auto_update"] is True  # the global is never mutated
    # A junk override is ignored rather than believed.
    assert cub_svc.merge_cub_settings(glob, {"auto_update": "no"})["auto_update"] is True


def test_summary_carries_auto_update(client, monkeypatch):
    _stub_backends(monkeypatch)
    monkeypatch.setattr(settings, "cub_auto_update", True, raising=False)
    body = client.get("/cub/summary").json()
    assert body["settings"]["auto_update"] is True

    monkeypatch.setattr(settings, "cub_auto_update", False, raising=False)
    body = client.get("/cub/summary").json()
    assert body["settings"]["auto_update"] is False


def test_summary_auto_update_honours_a_per_cub_override(client, monkeypatch):
    _stub_backends(monkeypatch)
    monkeypatch.setattr(settings, "cub_auto_update", True, raising=False)
    headers = {"X-Cub-Id": "cub-abc123"}
    assert client.get("/cub/summary", headers=headers).json()["settings"]["auto_update"] is True

    cub_svc.set_cub_overrides("cub-abc123", {"auto_update": False})
    body = client.get("/cub/summary", headers=headers).json()
    assert body["settings"]["auto_update"] is False
    # Fleet-wide is still on: only this one Cub opted out.
    assert cub_svc.global_cub_settings(settings)["auto_update"] is True


# -- the over-the-air image and manifest --------------------------------------

def _factory(app_body: bytes = b"\xe9payload") -> bytes:
    """A stand-in factory image: filler where the bootloader and partition
    table live, then an app at the offset a real one uses."""
    return b"\x00" * cub_svc.CUB_APP_OFFSET + app_body


def test_ota_image_is_the_app_slice_of_the_factory_image():
    assert cub_svc.ota_image_from_factory(_factory()) == b"\xe9payload"


def test_ota_image_rejects_anything_that_is_not_an_app():
    assert cub_svc.ota_image_from_factory(None) is None
    assert cub_svc.ota_image_from_factory(b"") is None
    assert cub_svc.ota_image_from_factory(b"\xe9short") is None       # too small
    assert cub_svc.ota_image_from_factory(_factory(b"nope")) is None  # no magic


def test_ota_block_points_at_an_absolute_path_and_hashes_the_app():
    import hashlib
    block = cub_svc.firmware_ota_block("tdisplay", _factory())
    assert block == {"path": "/cub/firmware/tdisplay.ota.bin",
                     "md5": hashlib.md5(b"\xe9payload").hexdigest()}
    assert cub_svc.firmware_ota_block("tdisplay", b"junk") is None


def test_manifest_carries_the_ota_block_when_there_is_an_image():
    block = cub_svc.firmware_ota_block("tdisplay", _factory())
    m = cub_svc.firmware_manifest("tdisplay", "1.2.3", ota=block)
    assert m["version"] == "1.2.3"
    build = m["builds"][0]
    assert build["chipFamily"] == "ESP32"
    assert build["ota"] == block
    # The browser flasher's own part is untouched by any of this.
    assert build["parts"] == [{"path": "tdisplay.bin", "offset": 0}]


def test_manifest_without_an_image_still_flashes():
    m = cub_svc.firmware_manifest("tdisplay", "1.2.3", ota=None)
    assert "ota" not in m["builds"][0]
    assert m["builds"][0]["parts"] == [{"path": "tdisplay.bin", "offset": 0}]
    assert cub_svc.firmware_manifest("nosuchboard", "1.2.3") is None


def test_manifest_endpoint_serves_a_locally_built_image(client, monkeypatch):
    from app.config import APP_VERSION
    local = cub_svc.local_override_path(settings.data_dir, "tdisplay")
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(_factory())

    body = client.get("/cub/firmware/manifest.json", params={"profile": "tdisplay"}).json()
    assert body["version"] == APP_VERSION
    assert body["builds"][0]["ota"] == cub_svc.firmware_ota_block("tdisplay", _factory())

    # The path in the manifest is the one that serves the app image.
    r = client.get("/cub/firmware/tdisplay.ota.bin")
    assert r.status_code == 200
    assert r.content == b"\xe9payload"

    assert client.get("/cub/firmware/manifest.json",
                      params={"profile": "nosuchboard"}).status_code == 404
    assert client.get("/cub/firmware/nosuchboard.ota.bin").status_code == 404


def test_firmware_is_reachable_from_the_lan_without_a_key(client, monkeypatch):
    """A Cub cannot present a key when it fetches firmware: the ESPHome update
    component sends no headers. So the two firmware GETs answer the local
    network on a password-protected install, and nothing else does."""
    from app.routers import cub as cub_router

    async def _none(*a, **k):
        return None, "no_release_asset"
    monkeypatch.setattr(cub_router, "_fetch_release_firmware", _none)
    monkeypatch.setattr(settings, "auth_password", "hunter2", raising=False)
    _stub_backends(monkeypatch)

    # The TestClient's host ("testclient") is not a private address, so an
    # off-LAN caller with no key is what this asks for first.
    assert client.get("/cub/firmware/manifest.json",
                      params={"profile": "tdisplay"}).status_code == 401

    monkeypatch.setattr("app.main.pairing_svc.is_private_address", lambda host: True)
    assert client.get("/cub/firmware/manifest.json",
                      params={"profile": "tdisplay"}).status_code == 200
    # Nothing published yet, but the request got past auth to say so.
    assert client.get("/cub/firmware/tdisplay.ota.bin").status_code == 404
    # Being on the LAN buys nothing anywhere else: the summary still needs the
    # Cub's key.
    assert client.get("/cub/summary").status_code == 401
