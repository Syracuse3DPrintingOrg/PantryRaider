"""Two-pane split view for ultrawide/tall kiosk panels (FoodAssistant-dqpq).

A Waveshare 480x1920 style bar panel shows two app pages at once: /ui/split
renders a minimal wrapper of two same-origin iframes (side by side in
landscape, stacked in portrait), the kiosk boot picks it up through a
server-side redirect in the start route, and the screensaver loads on the
wrapper so its overlay spans both panes.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings, _SAVEABLE, SATELLITE_PULL_FIELDS  # noqa: E402


# -- settings wiring -----------------------------------------------------------


def test_split_settings_defaults_and_device_local():
    s = type(settings)()
    assert s.kiosk_split_enabled is False       # off by default
    assert s.kiosk_split_primary == "start"     # Glance as the status pane
    assert s.kiosk_split_secondary == "add"     # Manage as the working pane
    assert s.kiosk_split_lock_primary is True   # the status pane holds
    for key in ("kiosk_split_enabled", "kiosk_split_primary",
                "kiosk_split_secondary", "kiosk_split_lock_primary"):
        assert key in _SAVEABLE, f"{key} missing from _SAVEABLE"
        # The split exists because of the panel's shape, so like the rest of
        # the kiosk display settings it never syncs from the main server.
        assert key not in SATELLITE_PULL_FIELDS, f"{key} should be device-local"


def test_setup_payload_accepts_split_fields():
    from app.routers.setup import SetupPayload

    p = SetupPayload(kiosk_split_enabled=True, kiosk_split_primary="start",
                     kiosk_split_secondary="cook", kiosk_split_lock_primary=False)
    assert p.kiosk_split_enabled is True
    assert p.kiosk_split_secondary == "cook"
    assert p.kiosk_split_lock_primary is False
    # Absent from the request = absent from the applied fields, so a partial
    # save (another pane's Save button) never clobbers the stored values.
    empty = SetupPayload().model_dump(exclude_unset=True)
    for key in ("kiosk_split_enabled", "kiosk_split_primary",
                "kiosk_split_secondary", "kiosk_split_lock_primary"):
        assert key not in empty


def test_split_pane_page_catalog_and_resolution():
    from app.navigation import split_pane_pages, split_pane_href

    pages = split_pane_pages()
    keys = {p["key"] for p in pages}
    # The catalog is the nav registry's real pages: every entry has a href,
    # and the two pane defaults are in it.
    assert "start" in keys and "add" in keys
    assert all(p["href"] for p in pages)
    assert all(not p["href"].startswith("http") for p in pages)
    # A known key resolves to its page; an unknown or stale key falls back to
    # the pane's default rather than a dead pane.
    assert split_pane_href("start", "add") == "ui/start"
    assert split_pane_href("nonsense", "add") == "ui/add"
    assert split_pane_href("", "start") == "ui/start"


# -- routes --------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "test-key", raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_split_page_renders_configured_panes(client, monkeypatch):
    monkeypatch.setattr(settings, "kiosk_split_primary", "start", raising=False)
    monkeypatch.setattr(settings, "kiosk_split_secondary", "cook", raising=False)
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/ui/split")
        assert r.status_code == 200
        assert 'id="pane-primary"' in r.text
        assert 'id="pane-secondary"' in r.text
        assert 'src="ui/start?in_split=1"' in r.text
        assert 'src="ui/cook?in_split=1"' in r.text
        # X-Frame-Options is SAMEORIGIN (main.py security headers), so the
        # framed pages must be same-origin paths: relative, no scheme or host.
        assert 'src="http' not in r.text
        assert r.headers.get("x-frame-options") == "SAMEORIGIN"


def test_split_page_preserves_the_kiosk_flag_in_both_panes(client):
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/ui/split?kiosk=1")
        assert r.status_code == 200
        # Jinja autoescape renders the & between params as &amp;, which the
        # browser reads back as a plain &.
        assert 'src="ui/start?in_split=1&amp;kiosk=1"' in r.text
        assert 'src="ui/add?in_split=1&amp;kiosk=1"' in r.text


def test_split_page_falls_back_on_a_stale_pane_key(client, monkeypatch):
    # A page key that no longer exists (renamed in an update, hand-edited
    # settings.json) must render the pane's default page, never a dead frame.
    monkeypatch.setattr(settings, "kiosk_split_primary", "gone", raising=False)
    monkeypatch.setattr(settings, "kiosk_split_secondary", "also-gone", raising=False)
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/ui/split")
        assert r.status_code == 200
        assert 'src="ui/start?in_split=1"' in r.text
        assert 'src="ui/add?in_split=1"' in r.text


def test_split_wrapper_carries_screensaver_and_lock(client, monkeypatch):
    monkeypatch.setattr(settings, "screensaver_minutes", 7, raising=False)
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/ui/split")
        assert r.status_code == 200
        # The saver lives on the wrapper so it overlays BOTH panes.
        assert 'id="screensaver-config"' in r.text
        assert 'data-minutes="7"' in r.text
        assert "screensaver.js" in r.text
        assert "kiosk-idle.js" in r.text
        # The locked-by-default first pane is marked, and the snap-back plus
        # the activity relay are wired on the wrapper.
        assert 'data-locked="1"' in r.text
        assert "loc.replace(homeHref)" in r.text
        assert "wireActivity(" in r.text
        # Orientation drives the layout: portrait stacks, landscape splits.
        assert "orientation: portrait" in r.text
        assert "flex-direction: column" in r.text


def test_split_wrapper_unlocked_pane_is_marked(client, monkeypatch):
    monkeypatch.setattr(settings, "kiosk_split_lock_primary", False, raising=False)
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/ui/split")
        assert 'data-locked="0"' in r.text


def test_kiosk_start_redirects_to_split_when_enabled(client, monkeypatch):
    monkeypatch.setattr(settings, "kiosk_split_enabled", True, raising=False)
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/ui/start?kiosk=1", follow_redirects=False)
        assert r.status_code in (302, 303, 307)
        assert "/ui/split?kiosk=1" in r.headers.get("location", "")
        # A plain (non-kiosk) visit still gets Glance: the split is a kiosk
        # display arrangement, not a change to normal browsing.
        r = client.get("/ui/start")
        assert r.status_code == 200
        # And a pane INSIDE the split never bounces back out (no loop).
        r = client.get("/ui/start?kiosk=1&in_split=1", follow_redirects=False)
        assert r.status_code == 200


def test_kiosk_start_does_not_redirect_when_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "kiosk_split_enabled", False, raising=False)
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/ui/start?kiosk=1", follow_redirects=False)
        assert r.status_code == 200


# -- save validation -----------------------------------------------------------


def test_split_save_round_trips_and_validates_page_keys(client, monkeypatch):
    monkeypatch.setattr(settings, "kiosk_split_enabled", False, raising=False)
    monkeypatch.setattr(settings, "kiosk_split_primary", "start", raising=False)
    monkeypatch.setattr(settings, "kiosk_split_secondary", "add", raising=False)
    monkeypatch.setattr(settings, "kiosk_split_lock_primary", True, raising=False)
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.post("/setup/save", json={
            "kiosk_split_enabled": True,
            "kiosk_split_primary": "start",
            "kiosk_split_secondary": "shopping",
            "kiosk_split_lock_primary": False,
        })
        assert r.status_code == 200
        assert settings.kiosk_split_enabled is True
        assert settings.kiosk_split_secondary == "shopping"
        assert settings.kiosk_split_lock_primary is False
        # An unknown page key falls back to the pane's default, never garbage.
        client.post("/setup/save", json={"kiosk_split_primary": "warpdrive",
                                         "kiosk_split_secondary": "warpdrive"})
        assert settings.kiosk_split_primary == "start"
        assert settings.kiosk_split_secondary == "add"
        # A save that omits the fields leaves the stored values alone.
        client.post("/setup/save", json={"screensaver_minutes": 5})
        assert settings.kiosk_split_enabled is True


def test_screen_pane_offers_the_split_controls(client, monkeypatch):
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/setup")
        assert r.status_code == 200
        for cid in ("kiosk_split_enabled", "kiosk_split_primary",
                    "kiosk_split_secondary", "kiosk_split_lock_primary"):
            assert f'id="{cid}"' in r.text, cid
        assert "saveSplitView()" in r.text
        # The save function persists just its own fields (setup/display.js).
        js = (SERVICE / "app" / "static" / "js" / "setup" / "display.js").read_text()
        assert "async function saveSplitView" in js
        assert "kiosk_split_lock_primary" in js


# -- the screensaver stands down inside a pane ---------------------------------


def test_screensaver_js_never_idle_activates_inside_a_frame():
    js = (SERVICE / "app" / "static" / "js" / "screensaver.js").read_text()
    # The wrapper page owns the saver so it spans both panes; a framed page
    # must not dim just its own pane, and must not run a duplicate
    # external-wake poll per pane.
    assert "var FRAMED" in js
    assert "window.self !== window.top" in js
    assert "!FRAMED && (kiosk || ALL_CLIENTS) && IDLE_MS > 0" in js
    assert "!FRAMED && (kiosk || ALL_CLIENTS))" in js
