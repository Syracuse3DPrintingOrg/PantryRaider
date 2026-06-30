"""Satellite read-only panes (FoodAssistant-fbmk).

On a satellite (pi_remote) the AI, Recipes (Mealie), and Barcode settings are
server-managed (pulled each sync and dropped by /setup/save), so the matching
setup.html panes must render read-only with a "managed on the main server" note
instead of editable inputs and per-pane Save buttons. On a non-satellite the
same panes stay editable with their Save buttons.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

from app.config import settings  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app

    cwd = os.getcwd()
    os.chdir(_SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    # No auth so the page renders without a redirect.
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_password", "")
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _render_setup(client, monkeypatch, *, satellite: bool) -> str:
    # pi_remote is the satellite deployment mode (is_satellite drives features).
    monkeypatch.setattr(
        settings, "deployment_mode", "pi_remote" if satellite else "server"
    )
    # Treat the install as fully configured so the setup-redirect middleware is
    # a no-op and the full settings page (not the wizard) renders.
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/setup")
    assert r.status_code == 200
    return r.text


# Inputs that must become read-only / disabled on a satellite, one per pane:
#   gemini_api_key  -> AI pane (secret_input macro)
#   barcode_llm_fallback -> AI pane (Barcode enrichment)
#   mealie_base_url -> Recipes pane
#   barcode_autocheck_shopping -> Recipes pane (barcode setting)
def test_satellite_panes_are_read_only(client, monkeypatch):
    html = _render_setup(client, monkeypatch, satellite=True)

    # AI provider/model and key fields are locked.
    assert _attr_present(html, "vision_provider", "disabled")
    assert _attr_present(html, "gemini_api_key", "readonly")
    assert _attr_present(html, "barcode_enrichment", "disabled")
    assert _attr_present(html, "barcode_llm_fallback", "disabled")

    # Recipes / Mealie and the barcode shopping toggle are locked.
    assert _attr_present(html, "mealie_base_url", "readonly")
    assert _attr_present(html, "barcode_autocheck_shopping", "disabled")
    assert _attr_present(html, "recipe_source", "disabled")

    # The per-pane Save buttons are replaced with managed-on-server notes.
    # (the savePane* JS functions still exist; only the buttons that call them
    # via onclick are dropped.)
    assert 'onclick="savePaneAi(this)"' not in html
    assert 'onclick="savePaneRecipes(this)"' not in html
    assert "AI settings are managed on the main server" in html
    assert "Recipe settings are managed on the main server" in html


def test_non_satellite_panes_stay_editable(client, monkeypatch):
    html = _render_setup(client, monkeypatch, satellite=False)

    # The Save buttons are present (panes are editable).
    assert 'onclick="savePaneAi(this)"' in html
    assert 'onclick="savePaneRecipes(this)"' in html
    # And the managed-on-server notes are not shown.
    assert "AI settings are managed on the main server" not in html
    assert "Recipe settings are managed on the main server" not in html

    # Editable fields do not carry readonly/disabled.
    assert not _attr_present(html, "vision_provider", "disabled")
    assert not _attr_present(html, "barcode_llm_fallback", "disabled")
    assert not _attr_present(html, "mealie_base_url", "readonly")
    assert not _attr_present(html, "barcode_autocheck_shopping", "disabled")


def test_satellite_updates_card_detects_availability(client, monkeypatch):
    """A Pi Remote must passively detect a newer version (FoodAssistant-r7e6).
    The satellite Updates card wires a network-based check (admin/check-update,
    independent of the local git checkout) and auto-runs it on load, alongside
    the separate "Update now" OTA button."""
    html = _render_setup(client, monkeypatch, satellite=True)
    assert 'onclick="checkSatelliteUpdate(this)"' in html
    assert 'id="update-avail"' in html
    # The availability check runs on load and the OTA stays on its own button.
    assert "checkSatelliteUpdate(null)" in html
    assert 'onclick="checkForUpdates()"' in html
    assert ">Update now" in html


def test_non_satellite_updates_card_has_server_update_now(client, monkeypatch):
    """A plain server (non-Pi) gets an availability check plus a manual Update
    now that triggers Watchtower, and still shows the copy-paste fallback. It
    does NOT use the Pi host-bridge OTA (checkForUpdates)."""
    html = _render_setup(client, monkeypatch, satellite=False)
    assert 'onclick="updateServerNow(this)"' in html       # manual trigger
    assert 'onclick="checkSatelliteUpdate(this)"' in html   # availability check
    assert 'onclick="checkForUpdates()"' not in html        # not the Pi OTA path
    assert "Release notes" in html                          # release-notes link
    assert "docker compose pull" in html                    # command fallback kept


def test_pi_hosted_gets_the_in_app_ota(client, monkeypatch):
    """Pi Hosted appliances must also get the one-button in-app updater
    (FoodAssistant-tu0i), not just Pi Remote: both run the host bridge."""
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted")
    with patch.object(type(settings), "is_configured", lambda self: True):
        html = client.get("/setup").text
    assert 'onclick="checkForUpdates()"' in html        # Update now (OTA)
    assert 'onclick="checkSatelliteUpdate(this)"' in html
    assert "checkSatelliteUpdate(null)" in html         # auto-check on load
    # Pi Hosted is not a satellite, so its other panes stay editable.
    assert 'onclick="savePaneAi(this)"' in html


def _attr_present(html: str, element_id: str, attr: str) -> bool:
    """True when the element with id="<element_id>" carries the given bare
    attribute (readonly/disabled) before its tag closes. The check stays local
    to that one tag so an unrelated later occurrence cannot cause a false hit.
    """
    marker = f'id="{element_id}"'
    idx = html.find(marker)
    if idx == -1:
        raise AssertionError(f"element id={element_id!r} not found in setup.html")
    end = html.find(">", idx)
    assert end != -1
    return attr in html[idx:end]
