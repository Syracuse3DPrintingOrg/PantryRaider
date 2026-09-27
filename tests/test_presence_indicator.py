"""Kiosk presence indicator (FoodAssistant-99vy).

A small ghosted icon on the kiosk display that lights while the LD2410C
presence sensor reads someone, so a sensor install can be verified on the
glass without SSH. These tests pin the save wiring (Settings field, _SAVEABLE,
SetupPayload, the /setup/save round trip), the server-side script gate in
base.html, the Settings toggle on the screen pane, and the load-bearing lines
of the browser script itself. Pure logic: no Pi, bridge, or network needed.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parents[1]
SERVICE = REPO / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402

JS = SERVICE / "app" / "static" / "js" / "presence-indicator.js"
BASE = SERVICE / "app" / "templates" / "base.html"


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd(); os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://g", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "presence_indicator_enabled", True, raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


# -- save wiring ---------------------------------------------------------------

def test_setting_exists_defaults_true_and_is_saveable():
    """Default True is deliberate and harmless: the script renders nothing
    unless the bridge reports a readable sensor, so installs without one never
    see the indicator, while a fresh sensor install works with zero setup."""
    from app.routers.setup import SetupPayload
    from app.config import _SAVEABLE, SATELLITE_PULL_FIELDS, Settings

    assert "presence_indicator_enabled" in Settings.model_fields
    assert Settings.model_fields["presence_indicator_enabled"].default is True
    assert "presence_indicator_enabled" in _SAVEABLE
    assert "presence_indicator_enabled" in SetupPayload.model_fields
    # Device-local like the other display settings: never satellite-synced.
    assert "presence_indicator_enabled" not in SATELLITE_PULL_FIELDS


def test_setting_round_trips_setup_save(client):
    r = client.post("/setup/save", json={"presence_indicator_enabled": False})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert settings.presence_indicator_enabled is False
    # And back on again.
    r = client.post("/setup/save", json={"presence_indicator_enabled": True})
    assert r.status_code == 200
    assert settings.presence_indicator_enabled is True


def test_save_without_the_field_leaves_the_stored_value_alone(client, monkeypatch):
    """/setup/save applies only the posted fields (exclude_unset), so another
    section's Save button can never flip the indicator."""
    monkeypatch.setattr(settings, "presence_indicator_enabled", False, raising=False)
    r = client.post("/setup/save", json={"quiet_mode": True})
    assert r.status_code == 200
    assert settings.presence_indicator_enabled is False


# -- base.html script gate -------------------------------------------------------

def test_base_html_loads_the_script_only_when_enabled(client, monkeypatch):
    """The script tag is server-gated on the setting, so disabled installs ship
    zero extra bytes, and it carries the mandatory ?v= cache-buster."""
    monkeypatch.setattr(settings, "presence_indicator_enabled", True, raising=False)
    html = client.get("/ui/convert").text
    m = re.search(r'src="static/js/presence-indicator\.js(\?[^"]*)"', html)
    assert m, "presence-indicator.js missing from base.html when enabled"
    assert "v=" in m.group(1), "presence-indicator.js has no ?v= cache buster"

    monkeypatch.setattr(settings, "presence_indicator_enabled", False, raising=False)
    html = client.get("/ui/convert").text
    assert "presence-indicator.js" not in html, (
        "presence-indicator.js must not ship when the setting is off")


# -- the Settings toggle ---------------------------------------------------------

def test_toggle_renders_on_the_screen_pane_on_a_pi(client, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted", raising=False)
    with patch.object(type(settings), "is_configured", lambda self: True), \
         patch("app.services.readiness.gate_possible", return_value=False), \
         patch("app.routers.setup.is_raspberry_pi", return_value=True), \
         patch("app.templating.is_raspberry_pi", return_value=True):
        html = client.get("/setup").text
    assert 'id="presence_indicator_enabled"' in html
    # Next to the wake-on-presence controls, on the same Kiosk display panel.
    assert html.index('id="wake_on_presence"') < html.index('id="presence_indicator_enabled"')


def test_display_save_posts_the_toggle():
    """The Save display settings button posts an explicit field list; the
    toggle must be in it or flipping it would silently never persist."""
    src = (SERVICE / "app" / "static" / "js" / "setup" / "display.js").read_text()
    assert "presence_indicator_enabled" in src


# -- the browser script itself ---------------------------------------------------

def test_js_gates_polls_and_stays_untouchable():
    """Plain-text pins on the load-bearing lines of presence-indicator.js:
    the kiosk latch, the relative activity poll, the presence_readable mount
    gate, pointer-events none, and reduced-motion support."""
    src = JS.read_text()
    # Kiosk latch first: the script no-ops entirely off a kiosk.
    assert "localStorage.getItem('kioskMode') !== 'true'" in src
    # RELATIVE poll URL, like the other kiosk pollers, so ingress prefixes
    # survive; never a leading slash.
    assert "fetch('setup/kiosk/activity'" in src
    assert "'/setup/kiosk/activity'" not in src
    # Mounts only once a sensor has actually fired: readable alone is true on
    # every Pi (bare pull-down pin), so the gate also requires ever-high or a
    # live detection (FoodAssistant-77ao).
    assert "presence_readable !== true" in src
    assert "presence_ever_high === true" in src
    assert "presence_detected === true" in src
    # Lit state follows presence_detected.
    assert "presence_detected" in src
    # The indicator can never eat a touch.
    assert "pointer-events:none" in src
    # Reduced motion drops the pulse; opacity alone carries the signal.
    assert "prefers-reduced-motion" in src
    assert "animation:none" in src


def test_js_polls_every_two_seconds_and_unmounts_on_failure():
    src = JS.read_text()
    assert "POLL_MS = 2000" in src
    # A failing poll unmounts rather than freezing a stale reading.
    assert "unmount();" in src.split(".catch(")[1][:200]


def test_js_poll_hygiene_backoff_and_visibility(client=None):
    """Verifier findings (99vy review): the poll must follow the sibling
    idiom, chained setTimeout (never setInterval), skip fetches while the tab
    is hidden, and back off on answers that hide the indicator, so a remote
    viewer-session kiosk or a downed bridge is not hammered every 2 seconds
    forever."""
    src = JS.read_text()
    assert "setInterval(" not in src
    assert ".finally(schedule)" in src
    assert "document.hidden" in src
    assert "visibilitychange" in src
    assert "BACKOFF_MAX_MS = 30000" in src
    assert "Math.min(delay * 2, BACKOFF_MAX_MS)" in src


def test_js_hides_a_stale_reading_when_wake_on_presence_is_off():
    """The bridge's presence loop idles in mode "off", freezing the detected
    value, and a diagnostic that shows a stale value as live is worse than
    none (99vy review, medium)."""
    src = JS.read_text()
    assert "wake_on_presence === 'off'" in src


def test_js_offsets_past_a_left_docked_floating_nav():
    """A left-docked floating nav is a full-height opaque bar over the
    indicator's corner; floating-nav.js publishes --float-nav-left exactly so
    fixed widgets can move out of its way (99vy review, medium)."""
    src = JS.read_text()
    assert "--float-nav-left" in src


def test_start_page_carries_the_indicator_too(client, monkeypatch):
    """Glance (/ui/start) is the DEFAULT kiosk home and the resting page of an
    idle display, and it does not extend base.html, so it needs its own gated
    include or the walk-up diagnostic never shows where it matters most
    (99vy review, high)."""
    monkeypatch.setattr(settings, "start_page_enabled", True, raising=False)
    monkeypatch.setattr(settings, "presence_indicator_enabled", True, raising=False)
    html = client.get("/ui/start").text
    m = re.search(r'src="static/js/presence-indicator\.js(\?[^"]*)"', html)
    assert m, "presence-indicator.js missing from the start page when enabled"
    assert "v=" in m.group(1)
    monkeypatch.setattr(settings, "presence_indicator_enabled", False, raising=False)
    html = client.get("/ui/start").text
    assert "presence-indicator.js" not in html


def test_base_html_comment_documents_the_gate():
    """The include is annotated in base.html so the next reader knows the
    script self-gates on kiosk mode and sensor readability."""
    src = BASE.read_text()
    assert "{% if presence_indicator_enabled %}" in src
    idx = src.index("presence-indicator.js")
    assert "FoodAssistant-99vy" in src[max(0, idx - 600):idx]
