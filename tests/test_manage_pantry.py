"""Manage Pantry page: mode tabs, client adaptation, QR button.

The Add Food page became Manage Pantry (FoodAssistant-7ss1 / -foiu): four
mode tabs (add stock, consume, shopping list, audit) that ARE the shared
scanner mode, plus kiosk/phone adaptation and an Open on phone QR button.
The route stays /ui/add so old links keep working.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

# The page's client logic lives in a cacheable static file, not inline in
# add.html (FoodAssistant-3c7k), so behavior assertions read this alongside
# the rendered HTML.
PAGE_JS = (SERVICE / "app" / "static" / "js" / "manage-pantry.js").read_text()

from app.config import settings  # noqa: E402


def test_default_nav_label_is_manage():
    # The tab is labeled "Manage" (renamed from "Manage Pantry", FoodAssistant-
    # gg33). Key and href are identifiers and must not move (old links, saved nav
    # orders, and nav_hidden entries keep working because "add"/"ui/add" are kept).
    from app.navigation import NAV_TABS

    tab = next(t for t in NAV_TABS if t["key"] == "add")
    assert tab["label"] == "Manage"
    assert tab["key"] == "add"
    assert tab["href"] == "ui/add"
    assert tab["href"] == "ui/add"


def test_mode_tabs_match_scanner_modes():
    # The page's four tabs must be exactly the scanner modes, in the same
    # order the Stream Deck key cycles through.
    from app.services.scanner_mode import SCANNER_MODES

    assert SCANNER_MODES == ("inventory", "consume", "shopping", "audit")


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


def _page(client):
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/ui/add")
    assert r.status_code == 200
    return r.text


def test_old_route_serves_manage_pantry(client):
    html = _page(client)
    assert "Manage Pantry" in html
    assert "Add Food" not in html


def test_page_has_all_four_mode_tabs(client):
    html = _page(client)
    for mode in ("inventory", "consume", "shopping", "audit"):
        assert f'id="mode-tab-{mode}"' in html
        assert f'id="mode-pane-{mode}"' in html


def test_mode_tabs_wired_to_scanner_mode_endpoint(client):
    # Selecting a tab must POST the shared scanner mode, and the page must
    # poll it so an externally-changed mode (deck key) moves the tab. The
    # page ships that logic via its static script.
    html = _page(client)
    assert "static/js/manage-pantry.js" in html
    assert "pending/scanner-mode" in PAGE_JS
    assert "refreshMode" in PAGE_JS


def test_qr_button_present(client):
    # Open on phone uses the existing base.html #qrModal / GET /ui/qr.
    html = _page(client)
    assert 'id="openOnPhoneBtn"' in html
    assert 'data-bs-target="#qrModal"' in html


def test_kiosk_adaptation_hooks_present(client):
    # Kiosk mode is a client-side signal (localStorage kioskMode), so the
    # template must ship the hooks: the hint block, the hideable camera card,
    # and the camera capability check.
    html = _page(client)
    assert 'id="kiosk-scan-hint"' in html
    assert 'id="camera-scan-card"' in html
    assert "navigator.mediaDevices" in PAGE_JS
    assert "kioskMode" in PAGE_JS


def test_shopping_tab_always_available(client):
    # The shopping list is built in (Grocy holds it by default), so the pane
    # offers its controls with no Mealie configured at all.
    with patch.object(type(settings), "mealie_configured", lambda self: False):
        html = _page(client)
    assert 'id="shoppingNameInput"' in html
    assert 'id="shopping-unconfigured"' not in html


def test_audit_tab_links_existing_audit_flow(client):
    # The audit tab is a front door to the existing location-scoped audit
    # flow, not a duplicate of it: it must link /ui/audit and read
    # /audit/status rather than keep its own session.
    html = _page(client)
    assert 'href="ui/audit"' in html
    assert "audit/status" in PAGE_JS
