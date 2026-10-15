"""Small setup and Settings fixes found on a fresh server install.

The update line's version label, the Stream Deck config answer on a device
with no deck, the default Forager kitchen name inside a container, the
sign-in page right after the wizard, and the kiosk hand-off request.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "service"
sys.path.insert(0, str(SERVICE))

_cwd = os.getcwd()
os.chdir(SERVICE)
try:
    from app.config import settings  # noqa: E402
    from app.passwords import hash_secret  # noqa: E402
    from app.routers.setup import page as setup_page  # noqa: E402
    from app.routers.setup import streamdeck as sd  # noqa: E402
finally:
    os.chdir(_cwd)

JS = SERVICE / "app" / "static" / "js" / "setup"


# -- One "v" on the version -----------------------------------------------------

def test_update_lines_use_the_version_label():
    helpers = (JS / "helpers.js").read_text()
    updates = (JS / "devices-updates.js").read_text()
    assert "function versionLabel(v)" in helpers
    assert "replace(/^v/i, '')" in helpers
    for text in (helpers, updates):
        assert "(v' + d.latest" not in text
        assert "<strong>' + d.latest + '</strong>" not in text
    pane = (SERVICE / "app" / "templates" / "setup" / "_pane_backups.html").read_text()
    assert "(v{{ update_last_latest }} available)" not in pane


def test_backups_pane_strips_the_tag_v():
    from jinja2 import Environment
    tmpl = Environment().from_string(
        "(v{{ update_last_latest[1:] if (update_last_latest or '').startswith('v') "
        "else update_last_latest }} available)")
    assert tmpl.render(update_last_latest="v0.21.0") == "(v0.21.0 available)"
    assert tmpl.render(update_last_latest="0.21.3") == "(v0.21.3 available)"
    pane = (SERVICE / "app" / "templates" / "setup" / "_pane_backups.html").read_text()
    assert "update_last_latest[1:] if (update_last_latest or '').startswith('v')" in pane


# -- Stream Deck config on a device with no deck ------------------------------

def test_streamdeck_config_without_a_bridge_is_a_normal_answer(monkeypatch):
    def _no_bridge(**kwargs):
        def _refuse(request):
            raise httpx.ConnectError("refused", request=request)
        return httpx.AsyncClient(transport=httpx.MockTransport(_refuse), **kwargs)
    monkeypatch.setattr(sd, "bridge_client", _no_bridge)
    out = asyncio.run(sd.streamdeck_config_get())
    assert isinstance(out, dict)  # a 200, not a JSONResponse error
    assert out["ok"] is False and out["available"] is False


# -- The default Forager kitchen name -----------------------------------------

@pytest.mark.parametrize("name,want", [
    ("b1b88e03627c", ""),
    ("B1B88E03627C", ""),
    ("a" * 64, ""),
    ("kitchen-pi", "kitchen-pi"),
    ("", ""),
])
def test_kitchen_name_skips_a_container_id(monkeypatch, name, want):
    monkeypatch.setattr(setup_page, "device_hostname", lambda: name)
    assert setup_page._suggested_kitchen_name() == want


# -- After the wizard -----------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "gk", raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret("pw"), raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    from app.main import app
    try:
        yield TestClient(app, client=("192.168.1.20", 50000))
    finally:
        os.chdir(_cwd)


def test_login_says_setup_is_done_after_the_wizard(client):
    note = "Setup is done. Sign in with the password you just chose."
    assert note in client.get("/ui/login?setup=done").text
    assert note not in client.get("/ui/login").text


def test_wizard_hands_off_to_the_sign_in_note_and_skips_the_kiosk_call():
    wiz = (JS / "wizard.js").read_text()
    assert "'ui/login?setup=done'" in wiz
    # The kiosk hand-off request 401s once a new password is live, and only a
    # Pi has a kitchen screen to hand off to.
    i = wiz.index("fetch('setup/kiosk/navigate/request'")
    guard = wiz[max(0, i - 300):i]
    assert "SETUP_DATA.is_pi && !newPassword" in guard
