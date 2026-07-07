"""On-screen keyboard for kiosk touchscreens (FoodAssistant-wo9j)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings, _SAVEABLE, SATELLITE_PULL_FIELDS  # noqa: E402

OSK_JS = SERVICE / "app" / "static" / "js" / "osk.js"


def test_osk_enabled_is_device_local_and_on_by_default():
    # On by default: a kiosk touchscreen has no physical keyboard, so the
    # keyboard must work out of the box; the setting exists to turn it off on
    # kiosks that DO have one attached.
    assert type(settings)().osk_enabled is True
    assert "osk_enabled" in _SAVEABLE                 # persisted
    # An attached keyboard is a per-device fact, never synced from the server.
    assert "osk_enabled" not in SATELLITE_PULL_FIELDS


def test_setup_payload_accepts_osk_enabled():
    from app.routers.setup import SetupPayload

    p = SetupPayload(osk_enabled=False)
    assert p.osk_enabled is False
    # Absent from the request = absent from the applied fields, so a partial
    # save never clobbers the stored value.
    assert "osk_enabled" not in SetupPayload().model_dump(exclude_unset=True)


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


def test_osk_config_rendered_on_pages(client, monkeypatch):
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/ui/timers")
        assert r.status_code == 200
        assert 'id="osk-config"' in r.text
        assert 'data-enabled="true"' in r.text
        assert "static/js/osk.js" in r.text
        # The off state reaches the config div so the script can stand down.
        monkeypatch.setattr(settings, "osk_enabled", False, raising=False)
        r = client.get("/ui/timers")
        assert 'data-enabled="false"' in r.text


def test_osk_included_on_the_standalone_setup_page(client):
    # The setup page does not extend base.html but a kiosk lands on it (the
    # first-run wizard, the settings page) and has API keys and names to type.
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/setup")
        assert r.status_code == 200
        assert 'id="osk-config"' in r.text
        assert "static/js/osk.js" in r.text
        # The per-device switch sits with the other kiosk display settings.
        assert 'id="osk_enabled"' in r.text


def test_osk_included_on_the_standalone_kiosk_templates():
    # start, pin, and login do not extend base.html either; a locked or
    # unauthenticated kiosk lands on them and login needs typed credentials.
    tpl = SERVICE / "app" / "templates"
    for name in ("start.html", "pin.html", "login.html"):
        assert '{% include "_osk.html" %}' in (tpl / name).read_text(), name


def test_osk_js_is_kiosk_gated_and_setting_gated():
    js = OSK_JS.read_text()
    # Kiosk mode only: the same localStorage gate the screensaver and intro use.
    assert "localStorage.getItem('kioskMode') === 'true'" in js
    # The per-device setting turns it off for kiosks with a real keyboard.
    assert "osk-config" in js
    assert "data-enabled" in js


def test_osk_js_covers_the_promised_inputs_and_keys():
    js = OSK_JS.read_text()
    # input[type=text|search|number|email|url|password] and textarea.
    for t in ("'text'", "'search'", "'number'", "'email'", "'url'", "'password'"):
        assert t in js
    assert "TEXTAREA" in js
    # A number input gets a digits-only pad.
    assert "ROWS_NUMBER" in js
    # Shift (with symbols on the digit row), backspace, space, done, enter.
    for key in ("SHIFT", "BKSP", "SPACE", "DONE", "ENTER", "SHIFT_MAP"):
        assert key in js


def test_osk_js_types_like_a_real_keyboard():
    js = OSK_JS.read_text()
    # Caret insertion via execCommand, with a value-splice + input event
    # fallback so the app's vanilla JS listeners always fire.
    assert "execCommand('insertText'" in js
    assert "dispatchEvent" in js
    assert "new Event('input'" in js
    # Enter mirrors a real keydown first, then submits the input's form.
    assert "keydown" in js
    assert "requestSubmit" in js


def test_osk_js_stays_out_of_the_screensavers_way():
    js = OSK_JS.read_text()
    # Below the screensaver overlay (2147483000) and the intro (2147483100).
    assert "2147482000" in js
    # Hides the moment the saver covers the page.
    assert "ss-active" in js


def test_osk_keys_meet_the_touch_target_convention():
    # kiosk.css convention: nothing tappable under 48px.
    js = OSK_JS.read_text()
    assert "min-width:48px" in js
    assert "min-height:52px" in js
