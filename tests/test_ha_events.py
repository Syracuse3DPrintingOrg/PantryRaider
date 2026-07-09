"""Tests for the on-screen Home Assistant event channel (notifications + camera
pop-ups) and the device-local convert customisation + Convert nav tab."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import ha_events  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    ha_events.reset()
    yield
    ha_events.reset()


# -- store ------------------------------------------------------------------

def test_store_add_and_poll_since():
    i1 = ha_events.add_notification("hi", title="T", level="warning")
    i2 = ha_events.add_camera(name="Door", src="ui/camera/0/snapshot", seconds=15)
    assert i2 == i1 + 1
    out = ha_events.poll(0)
    assert out["last_id"] == i2 and len(out["events"]) == 2
    assert out["events"][0]["type"] == "notification" and out["events"][0]["level"] == "warning"
    assert out["events"][1]["type"] == "camera" and out["events"][1]["seconds"] == 15
    # Polling since the last id returns nothing new.
    assert ha_events.poll(i2)["events"] == []


def test_unknown_level_falls_back_to_info():
    ha_events.add_notification("x", level="bogus")
    assert ha_events.poll(0)["events"][0]["level"] == "info"


def test_ring_is_capped():
    for n in range(70):
        ha_events.add_notification(f"n{n}")
    out = ha_events.poll(0)
    assert len(out["events"]) <= ha_events._MAX_EVENTS
    # last_id keeps counting even though older events were pruned.
    assert out["last_id"] == 70


# -- endpoints --------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd(); os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://g", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "vision_provider", "gemini", raising=False)
    monkeypatch.setattr(settings, "gemini_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "streamdeck_cameras",
                        [{"name": "Front Door", "ha_entity": "camera.front"}], raising=False)
    monkeypatch.setattr(settings, "ha_camera_popup_seconds", 20, raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_notify_endpoint(client):
    r = client.post("/events/notify", json={"message": "Door", "level": "info"})
    assert r.json()["ok"] is True
    ev = client.get("/events/poll?since=0").json()["events"]
    assert ev and ev[-1]["message"] == "Door"


def test_notify_requires_content(client):
    assert client.post("/events/notify", json={}).json()["ok"] is False


def test_camera_popup_resolves_name_to_proxy(client):
    r = client.post("/events/camera-popup", json={"camera": "Front Door", "seconds": 12}).json()
    assert r["ok"] is True and r["camera"] == "Front Door"
    ev = client.get("/events/poll?since=0").json()["events"][-1]
    assert ev["type"] == "camera" and ev["src"] == "ui/camera/0/snapshot" and ev["seconds"] == 12


def test_camera_popup_unknown_falls_back_to_first(client):
    r = client.post("/events/camera-popup", json={"camera": "nope"}).json()
    assert r["ok"] is True and r["camera"] == "Front Door"


def test_camera_popup_no_cameras(client, monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_cameras", [], raising=False)
    assert client.post("/events/camera-popup", json={}).json()["ok"] is False


def test_camera_page_opens_requested_camera(client, monkeypatch):
    # FoodAssistant-f230: /ui/camera?cam= picks the initial camera (by name or
    # index) so a Stream Deck camera key opens the requested feed, not camera 0.
    monkeypatch.setattr(settings, "streamdeck_cameras", [
        {"name": "Front Door", "ha_entity": "camera.front"},
        {"name": "Garage", "ha_entity": "camera.garage"},
    ], raising=False)
    body = client.get("/ui/camera?cam=Garage").text
    assert "const INITIAL_INDEX = 1;" in body
    # Selecting by index works too.
    assert "const INITIAL_INDEX = 1;" in client.get("/ui/camera?cam=1").text
    # No selector falls back to the first camera.
    assert "const INITIAL_INDEX = 0;" in client.get("/ui/camera").text
    # An unknown name also falls back to the first camera.
    assert "const INITIAL_INDEX = 0;" in client.get("/ui/camera?cam=nope").text


def test_navigate_event_queued_and_polled(client):
    r = client.post("/events/navigate", json={"path": "ui/cook"}).json()
    assert r["ok"] is True and r["path"] == "ui/cook"
    ev = client.get("/events/poll?since=0").json()["events"][-1]
    assert ev["type"] == "navigate" and ev["path"] == "ui/cook"


def test_navigate_strips_leading_slash_and_keeps_query(client):
    r = client.post("/events/navigate", json={"path": "/ui/camera?cam=1"}).json()
    assert r["ok"] is True and r["path"] == "ui/camera?cam=1"


def test_navigate_rejects_external_and_scheme_targets(client):
    for bad in ("http://evil.com", "//evil.com", "javascript:alert(1)", "", "  "):
        assert client.post("/events/navigate", json={"path": bad}).json()["ok"] is False


def test_safe_nav_path_unit():
    from app.routers.events import safe_nav_path
    assert safe_nav_path("ui/cook") == "ui/cook"
    assert safe_nav_path("/ui/cook") == "ui/cook"
    assert safe_nav_path("http://x/y") == ""
    assert safe_nav_path("//x") == ""
    assert safe_nav_path("javascript:x") == ""
    assert safe_nav_path("") == ""


# -- deck-action confirmations (FoodAssistant-rdlo) ---------------------------


def test_add_confirmation_store_shape():
    ha_events.reset()
    ha_events.add_confirmation("Added Milk to shopping list")
    ev = ha_events.poll(0)["events"][-1]
    # A distinct type so the kiosk can show it even with HA events off, and a
    # success level so the toast reads as an "it worked".
    assert ev["type"] == "confirm"
    assert ev["level"] == "success"
    assert ev["message"] == "Added Milk to shopping list"


def test_confirm_endpoint_queues_a_confirmation(client):
    r = client.post("/events/confirm",
                    json={"message": "Added Milk to shopping list"})
    assert r.json()["ok"] is True
    ev = client.get("/events/poll?since=0").json()["events"][-1]
    assert ev["type"] == "confirm" and ev["level"] == "success"
    assert ev["message"] == "Added Milk to shopping list"


def test_confirm_endpoint_requires_content(client):
    assert client.post("/events/confirm", json={}).json()["ok"] is False


HAEVENTS_JS = SERVICE / "app" / "static" / "js" / "ha-events.js"


def _run_should_show(enabled, ev_type):
    src = HAEVENTS_JS.read_text()
    m = re.search(r"function shouldShow\(ev\) \{.*?\n  \}", src, re.S)
    assert m, "could not extract shouldShow from ha-events.js"
    script = ("var enabled = " + ("true" if enabled else "false") + ";\n"
              + m.group(0) + "\nconsole.log(JSON.stringify(shouldShow({type: "
              + json.dumps(ev_type) + "})));")
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_confirmation_shows_even_when_ha_events_off():
    # The gate is the whole point of rdlo: a deck confirmation shows whether HA
    # on-screen events are on or off; HA-origin events still obey the toggle.
    assert _run_should_show(False, "confirm") is True
    assert _run_should_show(False, "notification") is False
    assert _run_should_show(False, "camera") is False
    assert _run_should_show(True, "notification") is True
    assert _run_should_show(True, "confirm") is True


def test_ha_events_js_renders_confirmations_always():
    js = HAEVENTS_JS.read_text()
    # The script no longer bails when HA events are off; it filters per event.
    assert "function shouldShow(ev)" in js
    assert "ev.type === 'confirm'" in js
    # A confirmation renders through the same toast path as a notification.
    assert "ev.type === 'notification' || ev.type === 'confirm' || ev.type === 'warning'" in js


# -- device-health warnings (FoodAssistant-h28s) ------------------------------


def test_add_warning_store_shape():
    ha_events.reset()
    ha_events.add_warning("Under-voltage detected. Check the Pi power supply.",
                          title="Power warning", key="undervoltage", level="error")
    ev = ha_events.poll(0)["events"][-1]
    # A distinct type so the kiosk shows it even with HA events off, and a
    # warning/error level so the toast reads as an alert, not an "it worked".
    assert ev["type"] == "warning"
    assert ev["level"] == "error"
    assert ev["key"] == "undervoltage"
    assert ev["message"].startswith("Under-voltage")


def test_add_warning_clamps_unknown_level_to_warning():
    ha_events.reset()
    ha_events.add_warning("hot", level="success")  # not a valid alert level
    assert ha_events.poll(0)["events"][-1]["level"] == "warning"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_warning_shows_even_when_ha_events_off():
    # A device-health warning is a local alert, not HA traffic: it shows whether
    # on-screen HA events are on or off, just like a deck confirmation.
    assert _run_should_show(False, "warning") is True
    assert _run_should_show(True, "warning") is True
    # HA-origin events still obey the toggle.
    assert _run_should_show(False, "notification") is False


def test_warning_test_endpoint_queues_a_warning(client):
    r = client.post("/events/warning-test")
    assert r.json()["ok"] is True
    ev = client.get("/events/poll?since=0").json()["events"][-1]
    assert ev["type"] == "warning" and ev["key"] == "undervoltage"
    assert ev["level"] == "error"


def test_convert_tab_and_custom_rows(client, monkeypatch):
    from app.navigation import visible_tabs
    monkeypatch.setattr(settings, "nav_order", "", raising=False)
    monkeypatch.setattr(settings, "nav_hidden", "", raising=False)
    assert "convert" in [t["key"] for t in visible_tabs()]
    monkeypatch.setattr(settings, "convert_custom_rows",
                        [{"label": "Stick of butter", "value": "113 g"}], raising=False)
    html = client.get("/ui/convert").text
    assert "Stick of butter" in html and "My conversions" in html


# -- state-file sharing (FoodAssistant-0fho) ----------------------------------

@pytest.fixture
def shared_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    ha_events.reset()
    yield tmp_path
    ha_events.reset()


def _forget_in_memory_state():
    """Simulate a different worker process (or a restart): the module-level
    ring is back at its import-time default, only the file remains."""
    ha_events._events = []
    ha_events._next_id = 1
    ha_events._mtime = None


def test_events_are_shared_across_workers(shared_dir):
    i1 = ha_events.add_notification("dinner", level="success")
    assert (shared_dir / "ha_events.json").exists()
    _forget_in_memory_state()
    # A kiosk polling a worker that never saw the post still gets the event.
    out = ha_events.poll(0)
    assert out["last_id"] == i1
    assert out["events"][0]["message"] == "dinner"
    # The since-id contract holds across workers too: an event added through
    # "another worker" keeps counting from the shared id sequence.
    i2 = ha_events.add_camera(name="Door", src="ui/camera/0/snapshot")
    assert i2 == i1 + 1
    _forget_in_memory_state()
    assert [e["id"] for e in ha_events.poll(i1)["events"]] == [i2]


def test_corrupt_event_file_never_breaks_a_poll(shared_dir):
    ha_events.add_notification("hi")
    (shared_dir / "ha_events.json").write_text("{not json")
    # The in-memory ring is kept; the corrupt file never raises.
    assert ha_events.poll(0)["events"][0]["message"] == "hi"
    # A fresh worker facing only the corrupt file degrades to empty, no raise.
    _forget_in_memory_state()
    assert ha_events.poll(0) == {"events": [], "last_id": 0}


def test_unwritable_data_dir_degrades_to_in_memory(monkeypatch):
    monkeypatch.setattr(settings, "data_dir", "/nonexistent/nowhere", raising=False)
    _forget_in_memory_state()
    i = ha_events.add_notification("local only")
    assert ha_events.poll(0)["last_id"] == i
