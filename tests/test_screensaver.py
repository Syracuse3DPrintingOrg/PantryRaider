"""Kiosk screensaver setting: soft on-screen idle layer (FoodAssistant-y65x)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings, _SAVEABLE, SATELLITE_PULL_FIELDS  # noqa: E402


def test_screensaver_minutes_is_device_local_and_off_by_default():
    assert type(settings)().screensaver_minutes == 0      # off by default
    assert "screensaver_minutes" in _SAVEABLE             # persisted
    # A wall panel and a countertop screen want different idle behaviour, so
    # the value never syncs from the main server.
    assert "screensaver_minutes" not in SATELLITE_PULL_FIELDS


def test_setup_payload_accepts_screensaver_minutes():
    from app.routers.setup import SetupPayload

    p = SetupPayload(screensaver_minutes=10)
    assert p.screensaver_minutes == 10
    # Absent from the request = absent from the applied fields, so a partial
    # save never clobbers the stored value.
    assert "screensaver_minutes" not in SetupPayload().model_dump(exclude_unset=True)


def test_grocy_is_a_known_install_log_name():
    # The wizard's Grocy install window polls setup/logs/grocy
    # (FoodAssistant-n5ky), so the proxy must accept the name.
    from app.routers.setup import _LOG_NAMES

    assert "grocy" in _LOG_NAMES


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_setup_page_has_screensaver_test_button(client):
    # FoodAssistant-fiwc: the Display pane's Test button needs the screensaver
    # script (and its config div) on the standalone setup page, which does not
    # extend base.html.
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/setup")
        assert r.status_code == 200
        assert "testScreensaver()" in r.text          # the button + handler
        assert 'id="screensaver-config"' in r.text    # config the script reads
        assert "static/js/screensaver.js" in r.text   # the script itself


def test_screensaver_js_has_test_hook_and_camera_guard():
    js = (SERVICE / "app" / "static" / "js" / "screensaver.js").read_text()
    # FoodAssistant-fiwc: the settings Test button calls this global.
    assert "window.__screensaverTest" in js
    # FoodAssistant-ysf6: idle activation defers to an open camera view.
    assert r"ui\/camera" in js       # the camera page path check
    assert ".hae-cam" in js          # the ha-events camera pop-up overlay


def test_screensaver_config_rendered_on_pages(client, monkeypatch):
    with patch.object(type(settings), "is_configured", lambda self: True):
        monkeypatch.setattr(settings, "screensaver_minutes", 7, raising=False)
        r = client.get("/ui/timers")
        assert r.status_code == 200
        assert 'id="screensaver-config"' in r.text
        assert 'data-minutes="7"' in r.text
        assert "screensaver.js" in r.text


# -- floating kitchen timers on the saver (FoodAssistant-8c6m) ----------------


def test_screensaver_js_floats_timer_pills():
    js = (SERVICE / "app" / "static" / "js" / "screensaver.js").read_text()
    # The saver polls the shared registry only while it is showing, and counts
    # down locally between polls with the satellite-shareable formula.
    assert "fetch('timers'" in js
    assert "TIMER_POLL_MS" in js
    assert "deadline_epoch" in js
    # Pi 3 budget: at most six simulated pills, the rest fold into "+N more".
    assert "TIMER_CAP = 6" in js
    assert "' more'" in js
    # Real bounce physics: panel walls in layout units, equal-mass elastic
    # pill collisions, and a carom off the logo block in bounce mode.
    assert "layoutSize" in js
    assert "collideTimerPair" in js
    assert "collideTimerWithLogo" in js
    # A finished timer pulses red/amber and reads Done.
    assert "ss-timer-done" in js
    assert "ss-timer-pulse" in js
    # The automated physics probe samples the simulated bodies through this.
    assert "window.__screensaverTimers" in js


def test_screensaver_pills_animate_last_minute_and_done_stages():
    js = (SERVICE / "app" / "static" / "js" / "screensaver.js").read_text()
    # The stage animations run on the pill's inner face, never the physics
    # shell, so the drift and collisions stay deterministic.
    assert "ss-timer-face" in js
    # Last minute of a countdown: a gentle hop, toggled by the local tick and
    # cleared again if the timer is extended past a minute.
    assert "ss-timer-ending" in js
    assert "ss-timer-glow" in js
    assert "242,0,110" in js   # brand pink, not the retired hop
    assert "remaining <= 60" in js
    # Finished: a slow continuous spin layered with the red/amber pulse.
    assert "ss-timer-spin" in js
    assert "ss-timer-spin 5s linear infinite" in js
    # markTimerDone hands the hop off to the spin.
    assert "classList.remove('ss-timer-ending')" in js


def test_timers_page_has_clear_all_and_screensaver_buttons(client):
    # FoodAssistant-ax5f / FoodAssistant-19xu: Clear all (confirm names the
    # count, DELETE on the /timers collection) and the on-demand screensaver
    # start, which only appears when the saver script's hook is present.
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/ui/timers")
        assert r.status_code == 200
        assert 'id="timersClearAll"' in r.text
        assert "Clear all" in r.text
        assert 'fetch("timers", { method: "DELETE" })' in r.text
        assert "window.confirm(msg)" in r.text
        assert 'id="startScreensaver"' in r.text
        assert "window.__screensaverTest" in r.text


def test_screensaver_js_maps_timer_labels_to_food_icons():
    js = (SERVICE / "app" / "static" / "js" / "screensaver.js").read_text()
    # A "Pasta" timer should read as pasta from across the room: a pure
    # keyword-to-emoji map, with a stopwatch for labels that name no food.
    assert "TIMER_FOOD_ICONS" in js
    assert "timerFoodIcon" in js
    assert "TIMER_DEFAULT_ICON" in js
    # The deck's preset timer labels (Eggs, Pasta, Rice) must all hit.
    for token in ("egg:", "pasta:", "rice:", "chicken:", "coffee:"):
        assert token in js


# -- Stream Deck config push (idle timeout + display-off logo) ----------------


def test_streamdeck_logo_when_display_off_is_device_local():
    assert type(settings)().streamdeck_logo_when_display_off is True
    assert "streamdeck_logo_when_display_off" in _SAVEABLE
    # What this deck shows while its own display sleeps is a per-device
    # choice, like streamdeck_key_style: never synced from the main server.
    assert "streamdeck_logo_when_display_off" not in SATELLITE_PULL_FIELDS


def test_setup_payload_accepts_display_off_logo_toggle():
    from app.routers.setup import SetupPayload

    p = SetupPayload(streamdeck_logo_when_display_off=False)
    assert p.streamdeck_logo_when_display_off is False
    assert "streamdeck_logo_when_display_off" not in SetupPayload().model_dump(exclude_unset=True)


def test_stale_layout_key_in_settings_json_is_ignored():
    # The retired streamdeck_screensaver_layout may linger in settings.json on
    # updated devices; apply() only adopts _SAVEABLE keys, so it must be a
    # silent no-op, never a crash or a stray attribute.
    s = type(settings)()
    s.apply({"streamdeck_screensaver_layout": "below",
             "streamdeck_logo_when_display_off": False})
    assert s.streamdeck_logo_when_display_off is False
    assert "streamdeck_screensaver_layout" not in _SAVEABLE


def test_streamdeck_config_push_carries_idle_timeout_and_logo_choice():
    """The deck's idle blank timeout and display-off logo choice only reach
    the controller through config.toml, so the settings push must stamp them
    (FoodAssistant-3fdq, FoodAssistant-zttc)."""
    from app.services import satellite as sat

    merged = sat._merge_streamdeck_settings(
        {"rotation": 90}, "Boston", "f", "dark",
        idle_timeout_minutes=15, logo_when_display_off=False,
    )
    assert merged["idle_timeout_minutes"] == 15
    assert merged["logo_when_display_off"] is False
    assert merged["rotation"] == 90
    # Defaults keep a bare merge safe: timeout off, the logo face on.
    merged = sat._merge_streamdeck_settings({}, "B", "f", "dark")
    assert merged["idle_timeout_minutes"] == 0
    assert merged["logo_when_display_off"] is True


# -- screensaver on any instance (FoodAssistant-xlb3) --------------------------


def test_screensaver_all_clients_is_device_local_and_off_by_default():
    assert type(settings)().screensaver_all_clients is False
    assert "screensaver_all_clients" in _SAVEABLE
    # Like the rest of the screensaver settings, each install decides for
    # itself; the value never syncs from the main server.
    assert "screensaver_all_clients" not in SATELLITE_PULL_FIELDS


def test_setup_payload_accepts_screensaver_all_clients():
    from app.routers.setup import SetupPayload

    p = SetupPayload(screensaver_all_clients=True)
    assert p.screensaver_all_clients is True
    assert "screensaver_all_clients" not in SetupPayload().model_dump(exclude_unset=True)


def test_screensaver_config_carries_all_clients_flag(client, monkeypatch):
    with patch.object(type(settings), "is_configured", lambda self: True):
        monkeypatch.setattr(settings, "screensaver_all_clients", True, raising=False)
        r = client.get("/ui/timers")
        assert r.status_code == 200
        assert 'data-all-clients="true"' in r.text
        monkeypatch.setattr(settings, "screensaver_all_clients", False, raising=False)
        r = client.get("/ui/timers")
        assert 'data-all-clients="false"' in r.text


def test_screensaver_js_gates_idle_on_kiosk_or_all_clients():
    js = (SERVICE / "app" / "static" / "js" / "screensaver.js").read_text()
    assert "data-all-clients" in js
    # Idle activation: kiosk OR the all-clients setting, still timeout-gated.
    assert "(kiosk || ALL_CLIENTS) && IDLE_MS > 0" in js
