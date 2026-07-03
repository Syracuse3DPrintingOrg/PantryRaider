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


# -- shared canvas with the Stream Deck (FoodAssistant-3fdq) -----------------


def test_streamdeck_screensaver_layout_is_device_local():
    from app.config import STREAMDECK_SCREENSAVER_LAYOUTS

    assert type(settings)().streamdeck_screensaver_layout == "off"
    assert "streamdeck_screensaver_layout" in _SAVEABLE
    # The deck's physical position next to THIS panel is a per-device fact,
    # like streamdeck_key_style: never synced from the main server.
    assert "streamdeck_screensaver_layout" not in SATELLITE_PULL_FIELDS
    assert STREAMDECK_SCREENSAVER_LAYOUTS == ("off", "above", "below", "left", "right")


def test_setup_payload_accepts_deck_layout_and_validates_it():
    from app.routers.setup import SetupPayload

    p = SetupPayload(streamdeck_screensaver_layout="below")
    assert p.streamdeck_screensaver_layout == "below"
    assert "streamdeck_screensaver_layout" not in SetupPayload().model_dump(exclude_unset=True)


def test_streamdeck_grid_aspect_matches_the_key_grids():
    from app.config import streamdeck_grid_aspect

    assert streamdeck_grid_aspect(6) == 2 / 3
    assert streamdeck_grid_aspect(15) == 3 / 5
    assert streamdeck_grid_aspect(32) == 4 / 8
    # Unknown size assumes the common 15-key deck.
    assert streamdeck_grid_aspect(0) == 3 / 5


def test_screensaver_state_freshness_is_pure():
    from app.services.screensaver_state import is_fresh, STALE_AFTER_SECONDS

    assert not is_fresh(0.0, 100.0)              # never posted
    assert is_fresh(100.0, 100.0)                # just posted
    assert is_fresh(100.0, 100.0 + STALE_AFTER_SECONDS)
    assert not is_fresh(100.0, 111.0)            # kiosk went away
    assert not is_fresh(200.0, 100.0 - 1)        # clock stepped backwards


def test_screensaver_state_round_trip_and_staleness():
    from app.services import screensaver_state as st

    st.reset()
    assert st.snapshot()["active"] is False
    st.update(True, x=0.4, y=1.05, w=0.2, h=0.1, band=0.3, layout="below")
    snap = st.snapshot()
    assert snap["active"] is True
    assert snap == {"active": True, "x": 0.4, "y": 1.05, "w": 0.2, "h": 0.1,
                    "band": 0.3, "layout": "below"}
    # The same state read past the staleness window counts as inactive, so a
    # dead kiosk never leaves the deck frozen mid-logo.
    import time as _time
    assert st.snapshot(now=_time.time() + 60)["active"] is False
    st.update(False)
    assert st.snapshot()["active"] is False
    st.reset()


def test_screensaver_dismiss_is_delivered_once():
    from app.services import screensaver_state as st

    st.reset()
    st.update(True, band=0.3, layout="below")
    st.dismiss()  # a deck key press
    # The kiosk's next post picks the mark up exactly once.
    assert st.update(True, band=0.3, layout="below")["dismiss"] is True
    assert st.update(True, band=0.3, layout="below")["dismiss"] is False
    st.reset()


def test_screensaver_state_endpoints(client):
    from app.services import screensaver_state as st

    st.reset()
    with patch.object(type(settings), "is_configured", lambda self: True):
        _exercise_state_endpoints(client)
    st.reset()


def _exercise_state_endpoints(client):
    r = client.post("/ui/screensaver/state", json={
        "active": True, "x": 0.4, "y": 1.05, "w": 0.2, "h": 0.1,
        "band": 0.3, "layout": "below"})
    assert r.status_code == 200 and r.json()["dismiss"] is False
    snap = client.get("/ui/screensaver/state").json()
    assert snap["active"] is True and snap["y"] == 1.05
    # A deck key press dismisses; the kiosk's next post is told to hide.
    assert client.post("/ui/screensaver/dismiss").json()["ok"] is True
    r = client.post("/ui/screensaver/state", json={"active": True})
    assert r.json()["dismiss"] is True
    # Garbage input is a safe no-op state, never a 500.
    r = client.post("/ui/screensaver/state", json={"x": "NaN", "active": 1})
    assert r.status_code == 200


def test_streamdeck_config_push_carries_idle_timeout_and_layout():
    """The deck's idle blank timeout and screensaver position only reach the
    controller through config.toml. The timeout was saved in app settings but
    never written there, so the deck never blanked (FoodAssistant-3fdq)."""
    from app.services import satellite as sat

    merged = sat._merge_streamdeck_settings(
        {"rotation": 90}, "Boston", "f", "dark",
        idle_timeout_minutes=15, screensaver_layout="below",
    )
    assert merged["idle_timeout_minutes"] == 15
    assert merged["screensaver_layout"] == "below"
    assert merged["rotation"] == 90
    # Defaults keep a bare merge safe: timeout off, deck out of the canvas.
    merged = sat._merge_streamdeck_settings({}, "B", "f", "dark")
    assert merged["idle_timeout_minutes"] == 0
    assert merged["screensaver_layout"] == "off"


def test_screensaver_config_div_carries_deck_layout(client, monkeypatch):
    with patch.object(type(settings), "is_configured", lambda self: True):
        monkeypatch.setattr(settings, "has_streamdeck", True, raising=False)
        monkeypatch.setattr(settings, "streamdeck_key_count", 15, raising=False)
        monkeypatch.setattr(settings, "streamdeck_screensaver_layout", "below",
                            raising=False)
        r = client.get("/ui/timers")
        assert r.status_code == 200
        assert 'data-deck-layout="below"' in r.text
        assert 'data-deck-aspect="0.6"' in r.text
        # Without a deck the layout renders as off regardless of the setting.
        monkeypatch.setattr(settings, "has_streamdeck", False, raising=False)
        r = client.get("/ui/timers")
        assert 'data-deck-layout="off"' in r.text
