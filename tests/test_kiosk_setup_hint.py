"""Kiosk first-time setup hint (FoodAssistant-cssj).

On the attached kiosk display the setup wizard's many text inputs are painful
to fill with a touchscreen, so when /setup is opened in kiosk mode before setup
is finished the page should steer the user to a phone/PC browser (with a LAN URL
and a QR code) instead of showing the wizard up front. Off kiosk, or once
configured, the hint must not appear.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

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
    # Unconfigured: the wizard (not the settings page) renders.
    monkeypatch.setattr(settings, "grocy_base_url", "", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "", raising=False)
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_kiosk_setup_shows_phone_hint(client):
    html = client.get("/setup?kiosk=1").text
    assert 'id="kiosk-setup-hint"' in html
    assert "Finish setup from your phone or computer" in html
    # The QR encodes a LAN setup URL, and the wizard starts hidden behind it.
    assert "ui/qr?url=" in html
    assert "/setup" in html
    assert 'id="wizard-root" class="wiz-wrap d-none"' in html or 'wiz-wrap d-none" id="wizard-root"' in html


def test_non_kiosk_setup_has_no_phone_hint(client):
    html = client.get("/setup").text
    assert 'id="kiosk-setup-hint"' not in html
    assert "Finish setup from your phone or computer" not in html


def test_configured_kiosk_has_no_phone_hint(client, monkeypatch):
    # Once configured, /setup is the settings page, not the wizard, so the hint
    # (which lives in the wizard branch) must not render even in kiosk mode.
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    html = client.get("/setup?kiosk=1").text
    assert 'id="kiosk-setup-hint"' not in html
