"""Settings Overview landing + Security change-password gate (FoodAssistant-jcnh).

The Stripe-style Settings rebuild added an Overview landing: a grid of category
cards grouped under the four headings, shown or hidden by deployment mode the
same way the menu pills are. The Security pane moved the change-password fields
behind a Change action; the backend current-password check that guards a set
password is unchanged, and these tests pin both behaviours.
"""
from __future__ import annotations

import os
import re
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
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_password", "")
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _render(client, monkeypatch, *, mode: str, is_pi: bool) -> str:
    monkeypatch.setattr(settings, "deployment_mode", mode)
    with patch.object(type(settings), "is_configured", lambda self: True), \
         patch("app.routers.setup.is_raspberry_pi", return_value=is_pi), \
         patch("app.templating.is_raspberry_pi", return_value=is_pi):
        r = client.get("/setup")
    assert r.status_code == 200
    return r.text


def _overview_region(html: str) -> str:
    """The Overview pane markup, from its pane div to the next pane div."""
    assert 'id="pane-overview"' in html, "Overview pane missing"
    return html.split('id="pane-overview"', 1)[1].split('id="pane-', 1)[0]


# Cards every configured install shows, mapped to the pane each opens.
_ALWAYS_CARDS = {
    "pane-personalization-recipes", "pane-scanning", "pane-screen",
    "pane-network", "pane-start-page", "pane-appearance",
    "pane-home-assistant", "pane-connections", "pane-devices",
    "pane-security", "pane-backups", "pane-advanced",
}
# Cards that only a main install shows (a satellite has no local inventory to
# configure and no local app to sign a Forager account into).
_MAIN_ONLY_CARDS = {"pane-inventory", "pane-forager"}


def test_overview_is_the_landing(client, monkeypatch):
    """Overview opens by default: its pill and pane both carry active, and the
    previous default (Appearance) does not."""
    for mode, is_pi in (("server", False), ("pi_hosted", True), ("pi_remote", True)):
        html = _render(client, monkeypatch, mode=mode, is_pi=is_pi)
        assert re.search(
            r'class="nav-link active[^"]*"[^>]*data-bs-target="#pane-overview"', html), mode
        assert re.search(
            r'<div class="tab-pane fade show active" id="pane-overview">', html), mode
        # Appearance is no longer the default-active pane.
        assert 'class="tab-pane fade show active" id="pane-appearance"' not in html, mode
        # The four group headings frame the cards, mirroring the side menu.
        region = _overview_region(html)
        for heading in ("Kitchen", "This Device", "Connections", "System"):
            assert f">{heading}<" in region, (mode, heading)


def test_overview_cards_gated_by_mode(client, monkeypatch):
    for mode, is_pi in (("server", False), ("pi_hosted", True), ("pi_remote", True)):
        region = _overview_region(_render(client, monkeypatch, mode=mode, is_pi=is_pi))
        panes = set(re.findall(r"openSettingsPane\('([^']+)'\)", region))
        want = set(_ALWAYS_CARDS)
        if mode != "pi_remote":
            want |= _MAIN_ONLY_CARDS
        assert panes == want, (mode, "missing", want - panes, "unexpected", panes - want)


def test_overview_helper_exists_in_menu_js(client):
    menu_js = client.get("static/js/setup/menu.js").text
    assert "function openSettingsPane(" in menu_js


def test_security_change_password_reveal(client, monkeypatch):
    """The change-password fields live behind a Change action, not as always-on
    paired fields; the underlying ids are kept so the save path still works."""
    html = _render(client, monkeypatch, mode="server", is_pi=False)
    sec = html.split('id="pane-security"', 1)[1].split('id="pane-', 1)[0]
    assert "revealSecretChange('change-password-block'" in sec
    assert 'id="change-password-block"' in sec
    assert 'id="auth_password"' in sec
    # It is the reveal helper, not an always-visible pair: the block is hidden.
    assert re.search(r'id="change-password-block" class="d-none', sec)
    # The helper is defined in the setup JS.
    assert "function revealSecretChange(" in client.get("static/js/setup/helpers.js").text


def test_security_change_password_gate_enforced(client, monkeypatch):
    """/setup/save still refuses to change a set password without the correct
    current password (FoodAssistant-f403), and accepts it when correct."""
    monkeypatch.setattr(settings, "deployment_mode", "server")
    monkeypatch.setattr(settings, "auth_password", "oldpass")  # legacy plaintext ok
    saved = {}
    monkeypatch.setattr(type(settings), "save", lambda self, d: saved.update(d))

    r = client.post("/setup/save", json={
        "auth_password": "newpass", "current_password": "wrong"})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "auth_password" not in saved  # nothing changed on a bad current password

    saved.clear()
    r = client.post("/setup/save", json={
        "auth_password": "newpass", "current_password": "oldpass"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert saved.get("auth_password") == "newpass"
