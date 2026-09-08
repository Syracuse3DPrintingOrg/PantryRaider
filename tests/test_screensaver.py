"""Kiosk screensaver setting: soft on-screen idle layer (FoodAssistant-y65x)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
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
    # A configured install has a connected inventory; without the key a
    # pi_hosted render is held on the first-boot gate (FoodAssistant-6v9q).
    monkeypatch.setattr(settings, "grocy_api_key", "test-key", raising=False)
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


# -- Ken Burns pan/zoom speed (FoodAssistant-nz62) ----------------------------


def test_ken_burns_speed_defaults_and_is_saveable():
    # A perceptible default: normal, not off; the fixed drift used to read as
    # nearly still on a small panel.
    assert type(settings)().screensaver_ken_burns_speed == "normal"
    assert "screensaver_ken_burns_speed" in _SAVEABLE
    # Per device, like the rest of the screensaver settings.
    assert "screensaver_ken_burns_speed" not in SATELLITE_PULL_FIELDS


def test_setup_payload_accepts_ken_burns_speed():
    from app.routers.setup import SetupPayload

    p = SetupPayload(screensaver_ken_burns_speed="fast")
    assert p.screensaver_ken_burns_speed == "fast"
    assert "screensaver_ken_burns_speed" not in SetupPayload().model_dump(exclude_unset=True)


def test_ken_burns_speed_save_rejects_unknown_value(client):
    # An unexpected value falls back to normal rather than persisting garbage.
    with patch.object(type(settings), "is_configured", lambda self: True):
        client.post("/setup/save", json={"screensaver_ken_burns_speed": "warp"})
        assert settings.screensaver_ken_burns_speed == "normal"
        client.post("/setup/save", json={"screensaver_ken_burns_speed": "fast"})
        assert settings.screensaver_ken_burns_speed == "fast"


def test_ken_burns_speed_in_screensaver_config(client, monkeypatch):
    with patch.object(type(settings), "is_configured", lambda self: True):
        monkeypatch.setattr(settings, "screensaver_ken_burns_speed", "fast", raising=False)
        r = client.get("/ui/timers")
        assert r.status_code == 200
        assert 'data-ken-burns-speed="fast"' in r.text


def test_ken_burns_speed_control_on_screen_pane(client, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted", raising=False)
    with patch.object(type(settings), "is_configured", lambda self: True), \
         patch("app.routers.setup.is_raspberry_pi", return_value=True), \
         patch("app.templating.is_raspberry_pi", return_value=True):
        r = client.get("/setup")
        assert r.status_code == 200
        assert 'id="screensaver_ken_burns_speed"' in r.text


def test_screensaver_js_scales_ken_burns_by_speed_and_respects_reduced_motion():
    js = (SERVICE / "app" / "static" / "js" / "screensaver.js").read_text()
    # The pan/zoom magnitude is driven by the per-speed multiplier.
    assert "KEN_BURNS_MAG" in js
    assert "data-ken-burns-speed" in js
    assert "kenBurnsFrame" in js
    # prefers-reduced-motion disables the pan/zoom, keeping only the crossfade.
    assert "prefers-reduced-motion" in js
    assert "REDUCED_MOTION" in js


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
    assert "PILL_SCALES" in js
    assert "KEN_BURNS" in js and "data-photo-seconds" in js and "data-pill-scale" in js
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


# -- cross-surface wake: dismiss on external activity (FoodAssistant-fho8) -----


SCREENSAVER_JS = SERVICE / "app" / "static" / "js" / "screensaver.js"


def test_screensaver_js_polls_bridge_activity_and_dismisses():
    js = SCREENSAVER_JS.read_text()
    # The kiosk half of the deck's cross-surface wake: poll the bridge's shared
    # activity (relayed by the app) and dismiss the saver when another surface
    # (a deck press) was just active.
    assert "setup/kiosk/activity" in js
    assert "externalWakeDue" in js
    assert "if (overlay) hide();" in js
    # Read-only: the ACTIVITY poll must never report activity itself, or it
    # would create a new wake source and defeat FoodAssistant-ofip. The only
    # POST in the file is the screensaver-state report (FoodAssistant-qh8p),
    # which goes to its own endpoint (never the activity endpoint) and the
    # bridge stores without touching the activity epoch, so it is not a wake
    # source. So: there is exactly one POST, and it is the screensaver report.
    assert js.count("method: 'POST'") == 1
    assert "setup/kiosk/screensaver" in js
    # No POST is ever aimed at the activity endpoint.
    assert not re.search(r"setup/kiosk/activity[^\n]*\n[^\n]*method:\s*'POST'", js)


def _run_external_wake(last_activity, now_epoch, prev_seen, window=None):
    src = SCREENSAVER_JS.read_text()
    m = re.search(r"function externalWakeDue\(lastActivity.*?\n  \}", src, re.S)
    assert m, "could not extract externalWakeDue from screensaver.js"
    args = [last_activity, now_epoch, prev_seen]
    if window is not None:
        args.append(window)
    script = ("var EXTERNAL_ACTIVITY_WINDOW_SECS = 12;\n" + m.group(0)
              + "\nconsole.log(JSON.stringify(externalWakeDue.apply(null, "
              + json.dumps(args) + ")));")
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_external_wake_recent_activity_dismisses():
    now = 1000.0
    # A press a moment ago: dismiss (wake) the saver, and adopt the epoch.
    res = _run_external_wake(now - 1, now, None)
    assert res["wake"] is True and res["seen"] == now - 1


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_external_wake_stale_or_missing_does_not_dismiss():
    now = 1000.0
    # Stale beyond the window on a first look: no dismiss.
    assert _run_external_wake(now - 300, now, None)["wake"] is False
    # A zero, missing, or future epoch is never a wake.
    assert _run_external_wake(0, now, None)["wake"] is False
    assert _run_external_wake(None, now, None)["wake"] is False
    assert _run_external_wake(now + 60, now, 5.0)["wake"] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_external_wake_advance_dismisses_even_when_poll_ran_late():
    now = 1000.0
    # The report aged past the window, but it advanced past the last-seen mark,
    # so a press happened between polls: still dismiss (mirrors the deck).
    res = _run_external_wake(now - 60, now, now - 300)
    assert res["wake"] is True and res["seen"] == now - 60
    # No advance and not fresh: stay asleep.
    assert _run_external_wake(now - 300, now, now - 300)["wake"] is False


# -- retro screensaver modes: flying toasters + starfield (FoodAssistant-umnk) -


def test_screensaver_modes_registered_with_bounce_default():
    from app.config import SCREENSAVER_MODES, _DEFAULT_SCREENSAVER_MODE

    # The originals plus the two retro canvas modes, all known values.
    for m in ("bounce", "photos", "toasters", "starfield"):
        assert m in SCREENSAVER_MODES
    # An unknown value falls back to the bouncing logo.
    assert _DEFAULT_SCREENSAVER_MODE == "bounce"
    assert type(settings)().screensaver_mode == "bounce"
    assert "screensaver_mode" in _SAVEABLE
    # Per device, like the rest of the screensaver settings.
    assert "screensaver_mode" not in SATELLITE_PULL_FIELDS


def test_setup_payload_accepts_screensaver_mode():
    from app.routers.setup import SetupPayload

    p = SetupPayload(screensaver_mode="toasters")
    assert p.screensaver_mode == "toasters"
    assert "screensaver_mode" not in SetupPayload().model_dump(exclude_unset=True)


def test_screensaver_mode_save_falls_back_to_bounce_on_unknown(client):
    # A stray value must never leave the panel on a mode the browser cannot
    # draw: an unknown mode persists as bounce, the known ones persist as-is.
    with patch.object(type(settings), "is_configured", lambda self: True):
        client.post("/setup/save", json={"screensaver_mode": "warpdrive"})
        assert settings.screensaver_mode == "bounce"
        for m in ("toasters", "starfield", "photos", "bounce"):
            client.post("/setup/save", json={"screensaver_mode": m})
            assert settings.screensaver_mode == m


def test_screensaver_mode_in_screensaver_config(client, monkeypatch):
    with patch.object(type(settings), "is_configured", lambda self: True):
        monkeypatch.setattr(settings, "screensaver_mode", "toasters", raising=False)
        r = client.get("/ui/timers")
        assert r.status_code == 200
        assert 'data-mode="toasters"' in r.text


def test_screen_pane_offers_the_retro_modes(client, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted", raising=False)
    with patch.object(type(settings), "is_configured", lambda self: True), \
         patch("app.routers.setup.is_raspberry_pi", return_value=True), \
         patch("app.templating.is_raspberry_pi", return_value=True):
        r = client.get("/setup")
        assert r.status_code == 200
        assert 'value="toasters"' in r.text
        assert "Flying toasters" in r.text
        assert 'value="starfield"' in r.text
        assert "Starfield" in r.text


def test_screensaver_js_registers_and_draws_the_retro_modes():
    js = SCREENSAVER_JS.read_text()
    # Modes normalized in the browser too, unknown falling back to bounce.
    assert "SCREENSAVER_MODES = ['bounce', 'photos', 'toasters', 'starfield']" in js
    assert "function normalizeMode" in js
    assert "window.__screensaverMode" in js
    # The canvas modes are dispatched and drawn programmatically (no assets).
    assert "startCanvasMode" in js
    assert "function drawToaster" in js
    assert "function drawStarfield" in js
    # Pantry Raider wink: a brand-pink toaster/star.
    assert "'#f2006e'" in js
    # One rAF loop that also steps the pills, and a fail-soft fall back to
    # bounce on a draw error.
    assert "stepTimerBodies(ts, w, h, null)" in js
    assert "startBounceMode();" in js
    # No external assets: the canvas modes never fetch or load an image.
    assert "new Image" not in js
    # Pi 3 budget + reduced-motion thinning, one shared cap helper.
    assert "screensaverSpriteBudget" in js
    assert "REDUCED_MOTION" in js


def _run_js_fn(name, extract_re, call_args, preamble=""):
    src = SCREENSAVER_JS.read_text()
    m = re.search(extract_re, src, re.S)
    assert m, "could not extract %s from screensaver.js" % name
    script = (preamble + "\n" + m.group(0)
              + "\nconsole.log(JSON.stringify(" + name + ".apply(null, "
              + json.dumps(call_args) + ")));")
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_normalize_mode_falls_back_to_bounce():
    rx = r"function normalizeMode\(m\).*?\n  \}"
    pre = "var SCREENSAVER_MODES = ['bounce', 'photos', 'toasters', 'starfield'];"
    assert _run_js_fn("normalizeMode", rx, ["toasters"], pre) == "toasters"
    assert _run_js_fn("normalizeMode", rx, ["starfield"], pre) == "starfield"
    assert _run_js_fn("normalizeMode", rx, ["photos"], pre) == "photos"
    assert _run_js_fn("normalizeMode", rx, ["nonsense"], pre) == "bounce"
    assert _run_js_fn("normalizeMode", rx, [None], pre) == "bounce"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_sprite_budget_caps_and_thins_for_reduced_motion():
    rx = r"function screensaverSpriteBudget\(kind, reduced\).*?\n  \}"
    # Toasters stay a dozen-ish; the field thins, never grows, for reduced
    # motion. Starfield is capped for a Pi 3 and thinned the same way.
    assert _run_js_fn("screensaverSpriteBudget", rx, ["toasters", False]) == 12
    assert _run_js_fn("screensaverSpriteBudget", rx, ["toasters", True]) == 7
    assert _run_js_fn("screensaverSpriteBudget", rx, ["starfield", False]) == 110
    assert _run_js_fn("screensaverSpriteBudget", rx, ["starfield", True]) == 40


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_sprite_offscreen_only_past_left_or_bottom():
    rx = r"function spriteOffscreen\(x, y, size, w, h\).*?\n  \}"
    W, H, S = 800, 480, 40
    # On screen: not offscreen.
    assert _run_js_fn("spriteOffscreen", rx, [400, 240, S, W, H]) is False
    # Flown past the left edge (down-left drift): respawn due.
    assert _run_js_fn("spriteOffscreen", rx, [-50, 240, S, W, H]) is True
    # Dropped below the bottom edge: respawn due.
    assert _run_js_fn("spriteOffscreen", rx, [400, 540, S, W, H]) is True
    # Off the top or right (where it enters) is NOT a respawn.
    assert _run_js_fn("spriteOffscreen", rx, [900, -60, S, W, H]) is False


# -- deck display-off logo on a soft (screensaver) sleep (FoodAssistant-qh8p) --


def test_screensaver_js_reports_overlay_state_edge_triggered():
    js = SCREENSAVER_JS.read_text()
    # show()/hide() report the overlay state so the deck can raise its
    # display-off logo while the kitchen screen sleeps under the overlay.
    assert "setup/kiosk/screensaver" in js
    assert "reportScreensaverState(true)" in js
    assert "reportScreensaverState(false)" in js
    # Only the real kiosk panel reports (never an all-clients browser), so a
    # saver on someone's phone can't put the deck to sleep.
    assert "if (!kiosk) return;" in js
    # Edge-triggered on show/hide, not a repeating poll: the report lives in
    # show()/hide(), not in a setInterval.
    assert "setInterval(reportScreensaverState" not in js


def test_kiosk_screensaver_relay_is_noop_off_pi(client, monkeypatch):
    # Off a Pi the relay reports the state back without a bridge call, so the
    # browser fetch never errors on a plain server or a desktop.
    monkeypatch.setattr("app.routers.setup.is_raspberry_pi", lambda: False)
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.post("/setup/kiosk/screensaver", json={"active": True})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["screensaver_active"] is True
        r = client.post("/setup/kiosk/screensaver", json={})
        assert r.json()["screensaver_active"] is False


# -- screensaver clock/overlay stay on-screen at 270 rotation (FoodAssistant-7irg) --


def test_screensaver_clock_uses_bounded_corner_helper():
    js = SCREENSAVER_JS.read_text()
    # A single pure helper places the corner clock, capped so it can never grow
    # wider than the panel (accounts for its own width) and inset from the edge,
    # so a 270-rotated panel no longer clips it off the right.
    assert "function cornerClockCss" in js
    assert "window.__cornerClockCss" in js
    assert "max-width:calc(100% - 8vmin)" in js
    # No screensaver element uses a viewport-overflowing width: the overlay and
    # photo layers use 100%/inset:0, never 100vw.
    assert "100vw" not in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_corner_clock_css_is_capped_and_anchored():
    rx = r"function cornerClockCss\(corner\).*?\n  \}"
    css = _run_js_fn("cornerClockCss", rx, ["right:3vmin;bottom:3vmin;"])
    assert "position:absolute" in css
    assert "right:3vmin" in css and "bottom:3vmin" in css
    # The width cap is what keeps the box inside the panel at every rotation.
    assert "max-width:calc(100% - 8vmin)" in css


# -- flying toasters: two wings + 3D chrome (FoodAssistant-umnk review) --------


def test_screensaver_toaster_is_two_winged_and_3d():
    js = SCREENSAVER_JS.read_text()
    # Two wings: a shared wing helper drawn once per side, the right one
    # mirrored with scale(-1,1), both driven by the same flap so they sync.
    assert "function drawToasterWing" in js
    assert js.count("drawToasterWing(ctx, wingFill, wingLine)") == 2
    assert "ctx.scale(-1, 1)" in js
    # 3/4 dimensional chrome: distinct front, receding side, and top faces so it
    # reads as a solid metallic object, not a flat silhouette.
    assert "var front =" in js
    assert "var side =" in js
    assert "var top =" in js
    # Shading is flat bands, never a per-frame canvas gradient (Pi 3 budget).
    assert "createLinearGradient" not in js
    # Still the Pantry Raider wink: a brand-pink toaster.
    assert "'#f2006e'" in js


# -- manual open must survive the external-wake poll (FoodAssistant-qh8p f/u) --


def test_screensaver_js_seeds_external_baseline_on_manual_open():
    js = SCREENSAVER_JS.read_text()
    # A manual open (Test / timers-menu start / launch key all go through
    # __screensaverTest) seeds the external-wake baseline and marks the saver
    # manually-open for its whole life, so the still-fresh activity from the tap
    # that opened it cannot dismiss it (not just for a short grace window).
    assert "ssSeedExternal = true;" in js
    assert "ssManualOpen = true;" in js
    # hide() clears the manual-open flag so the next open re-arms it.
    assert "ssManualOpen = false;" in js
    # The poll narrows dismissal through the manual-open-aware helper.
    assert "screensaverExternalWake(" in js
    assert "window.__screensaverExternalWake" in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_screensaver_external_wake_grace_ignores_stale_open_activity():
    src = SCREENSAVER_JS.read_text()
    ew = re.search(r"function externalWakeDue\(lastActivity.*?\n  \}", src, re.S)
    sw = re.search(r"function screensaverExternalWake\(lastActivity.*?\n  \}",
                   src, re.S)
    assert ew and sw, "could not extract the wake helpers from screensaver.js"
    pre = "var EXTERNAL_ACTIVITY_WINDOW_SECS = 12;\n" + ew.group(0) + "\n" + \
        sw.group(0)

    def run(la, now, prev, in_grace):
        script = (pre + "\nconsole.log(JSON.stringify(screensaverExternalWake("
                  + json.dumps(la) + "," + json.dumps(now) + ","
                  + json.dumps(prev) + "," + json.dumps(in_grace) + ")));")
        out = subprocess.run(["node", "-e", script], capture_output=True,
                             text=True, timeout=20)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    now = 1000.0
    # Manual open: the baseline is the tap that opened it (now-2, still inside
    # the 12s window). In grace, that stale-but-fresh stamp must NOT dismiss.
    assert run(now - 2, now, now - 2, True)["wake"] is False
    # Activity strictly after the open (an advance past the baseline) DOES
    # dismiss even in grace: a genuine touch or deck press (fho8 preserved).
    assert run(now - 1, now, now - 2, True)["wake"] is True
    # Outside the grace window it is exactly externalWakeDue: fresh dismisses.
    assert run(now - 2, now, now - 2, False)["wake"] is True
    # An idle stamp older than the window never dismisses, grace or not.
    assert run(now - 300, now, now - 2, True)["wake"] is False
