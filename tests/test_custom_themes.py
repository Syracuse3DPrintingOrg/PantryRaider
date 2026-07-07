"""Named custom themes and the Theme/Navigation split (FoodAssistant-nw49,
-py1o, -oret, -37gi, -bbz8).

Covers:
  * config-level resolution of a "custom:<id>" theme to its stored colours,
  * save() accepting a valid custom:<id> and rejecting a dangling one,
  * the /setup/custom-theme save + delete endpoints,
  * the Settings page rendering the split Theme/Navigation panes, the reset
    control, and the moved Display/Stream Deck pills under Personalization.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

from app.config import settings, theme_info, resolve_custom_colors  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app

    cwd = os.getcwd()
    os.chdir(_SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(settings, "custom_themes", [])
    monkeypatch.setattr(settings, "ui_theme", "dark")
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


_THEME = {
    "name": "My Kitchen", "base": "light",
    "primary": "#ff7700", "accent": "#00ddaa",
    "bg": "#101418", "surface": "#1c2228", "text": "#eef2f6",
}


# -- config resolution ------------------------------------------------------

def test_resolve_custom_colors_for_named_theme(monkeypatch):
    monkeypatch.setattr(settings, "custom_themes", [
        {"id": "my_kitchen", "name": "My Kitchen", "base": "light",
         "primary": "#ff7700", "accent": "#00ddaa", "bg": "#101418",
         "surface": "#1c2228", "text": "#eef2f6"},
    ])
    monkeypatch.setattr(settings, "ui_theme", "custom:my_kitchen")
    colors = resolve_custom_colors("custom:my_kitchen")
    assert colors["primary"] == "#ff7700"
    assert colors["base"] == "light"
    # theme_info follows the named theme's base for data-bs-theme.
    assert theme_info("custom:my_kitchen")["mode"] == "light"
    # A built-in theme is not a custom theme.
    assert resolve_custom_colors("dark") is None


def test_save_accepts_valid_custom_id_and_rejects_dangling(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "custom_themes", [])
    monkeypatch.setattr(settings, "ui_theme", "dark")
    # A custom:<id> with no matching theme is rejected back to the default.
    settings.save({"ui_theme": "custom:ghost"})
    assert settings.ui_theme != "custom:ghost"
    # Saved together with its theme, it sticks.
    settings.save({
        "custom_themes": [{"id": "ghost", "name": "Ghost", "base": "dark",
                           "primary": "#111111", "accent": "#222222",
                           "bg": "#000000", "surface": "#101010", "text": "#ffffff"}],
        "ui_theme": "custom:ghost",
    })
    assert settings.ui_theme == "custom:ghost"


# -- endpoints --------------------------------------------------------------

def test_custom_theme_save_and_delete_endpoints(client):
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.post("/setup/custom-theme", json=_THEME)
    assert r.status_code == 200 and r.json()["ok"] is True
    tid = r.json()["id"]
    assert settings.ui_theme == f"custom:{tid}"
    assert any(t["id"] == tid for t in settings.custom_themes)

    # Re-saving the same name updates in place (no duplicate).
    with patch.object(type(settings), "is_configured", lambda self: True):
        client.post("/setup/custom-theme", json={**_THEME, "primary": "#abcdef"})
    matches = [t for t in settings.custom_themes if t["id"] == tid]
    assert len(matches) == 1 and matches[0]["primary"] == "#abcdef"

    # Delete the active theme -> falls back off custom.
    with patch.object(type(settings), "is_configured", lambda self: True):
        d = client.post("/setup/custom-theme/delete", json={})
    assert d.json()["ok"] is True
    assert not settings.ui_theme.startswith("custom:")
    assert all(t["id"] != tid for t in settings.custom_themes)


def test_custom_theme_save_rejects_blank_name_and_bad_hex(client):
    with patch.object(type(settings), "is_configured", lambda self: True):
        assert client.post("/setup/custom-theme", json={**_THEME, "name": "  "}).json()["ok"] is False
        assert client.post("/setup/custom-theme", json={**_THEME, "primary": "red"}).json()["ok"] is False


# -- page structure ---------------------------------------------------------

def _render(client, monkeypatch, *, is_pi: bool) -> str:
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted" if is_pi else "server")
    with patch.object(type(settings), "is_configured", lambda self: True), \
         patch("app.routers.setup.is_raspberry_pi", return_value=is_pi), \
         patch("app.templating.is_raspberry_pi", return_value=is_pi):
        r = client.get("/setup")
    assert r.status_code == 200
    return r.text


def test_theme_and_navigation_share_the_appearance_pane(client, monkeypatch):
    html = _render(client, monkeypatch, is_pi=False)
    assert 'id="pane-appearance"' in html
    assert 'data-bs-target="#pane-appearance"' in html
    pane = html.split('id="pane-appearance"', 1)[1].split('id="pane-', 1)[0]
    # The named-theme builder and nav reset controls render in it.
    assert 'onclick="saveCustomTheme(this)"' in pane
    assert 'onclick="resetNavEditor(this)"' in pane
    assert "window.TABS_DEFAULT" in html


def test_saved_custom_theme_shows_in_dropdown(client, monkeypatch):
    monkeypatch.setattr(settings, "custom_themes", [
        {"id": "my_kitchen", "name": "My Kitchen", "base": "dark",
         "primary": "#ff7700", "accent": "#00ddaa", "bg": "#101418",
         "surface": "#1c2228", "text": "#eef2f6"},
    ])
    monkeypatch.setattr(settings, "ui_theme", "custom:my_kitchen")
    html = _render(client, monkeypatch, is_pi=False)
    assert 'value="custom:my_kitchen"' in html
    assert "My Kitchen" in html
    # Active custom theme is applied to the standalone Settings page too.
    assert "#ff7700" in html


def test_display_settings_live_in_screen_pane(client, monkeypatch):
    html = _render(client, monkeypatch, is_pi=True)
    # The kiosk panel settings live in the Screen & Sleep pane.
    assert 'data-bs-target="#pane-screen"' in html
    screen = html.split('id="pane-screen"', 1)[1].split('id="pane-', 1)[0]
    assert 'id="ui_scale"' in screen
    assert 'id="screensaver_minutes"' in screen
    # The Start Page & Stream Deck pill targets the Start Page; the deck
    # editor has no pill of its own and is reached via the in-pane toggle.
    assert 'data-bs-target="#pane-streamdeck"' not in html
    assert 'data-bs-target="#pane-start-page"' in html
    assert 'onclick="showDeckStart(\'deck\')"' in html
    assert "showDeckStart('start')" in html


def test_attached_hardware_in_devices_pane_not_streamdeck(client, monkeypatch):
    # After the IA reorg (FoodAssistant-42n4) this device's attached hardware
    # moved to the Network pane under This Device, out of the Fleet pane and
    # never in the Stream Deck editor.
    html = _render(client, monkeypatch, is_pi=True)
    net = html.split('id="pane-network"', 1)[1].split('id="pane-', 1)[0]
    assert "hwdetect-display" in net
    assert "Attached hardware" in net
    dev = html.split('id="pane-devices"', 1)[1].split('id="pane-', 1)[0]
    assert "hwdetect-display" not in dev
    # The Stream Deck pane is the last pane, so bound its segment at the end
    # of the tab content (before the page scripts, which mention the id too).
    sd = html.split('id="pane-streamdeck"', 1)[1].split("<script", 1)[0]
    assert "hwdetect-display" not in sd
