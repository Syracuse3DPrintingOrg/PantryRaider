"""Barcode-scanner setup wizard (FoodAssistant-udpk).

The wizard renders a reader's configuration codes in order so the reader can
program itself off the screen. These tests pin the code sequence for the
default reader, that the reader dropdown switches sequences, that the Scanning
settings pane exposes the launch button and the mobile-launch QR, and that the
QR endpoint returns an image without error. Pure/route tests, no network.
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
from app.services import scanner_wizard  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app

    cwd = os.getcwd()
    os.chdir(_SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_password", "")
    # The setup-redirect middleware bounces an unconfigured install to /setup,
    # so mark it configured to reach the wizard and QR routes directly.
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True)
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


# The determined hands-free setup, in the exact order the doc lists it
# (docs/hardware/waveshare-barcode-scanner.md).
_EXPECTED_DEFAULT_CODES = [
    "restore-factory",
    "sensing-mode",
    "sensing-nonscan-interval-500ms",
    "enable-same-barcode-delay",
    "same-barcode-delay-3000ms",
    "save-user-default",
]


def _positions(html: str, needles: list[str]) -> list[int]:
    return [html.index(n) for n in needles]


def test_default_wizard_renders_codes_in_order(client):
    r = client.get("/ui/scanner-setup")
    assert r.status_code == 200
    html = r.text
    # Every code image for the recommended sequence is present...
    for code in _EXPECTED_DEFAULT_CODES:
        assert f"scanner/waveshare/{code}.png" in html, code
    # ...and they render in the documented order.
    pos = _positions(html, [f"scanner/waveshare/{c}.png" for c in _EXPECTED_DEFAULT_CODES])
    assert pos == sorted(pos), "codes must render in the documented order"


def test_default_model_is_recommended_waveshare(client):
    r = client.get("/ui/scanner-setup")
    assert r.status_code == 200
    # The recommended reader is pre-selected in the picker.
    assert scanner_wizard.DEFAULT_MODEL_ID == "waveshare"
    assert re.search(r'<option value="waveshare"\s+selected', r.text)


def test_model_dropdown_switches_sequences(client):
    # The keyboard-fix sequence is a different, shorter set of codes.
    r = client.get("/ui/scanner-setup?model=waveshare_keyboard_fix")
    assert r.status_code == 200
    html = r.text
    assert "scanner/waveshare/usb-hid-device.png" in html
    assert "scanner/waveshare/hid-kbw.png" in html
    # The hands-free codes are not part of this sequence.
    assert "scanner/waveshare/sensing-mode.png" not in html

    # The generic reader has no on-screen codes at all.
    r = client.get("/ui/scanner-setup?model=generic")
    assert r.status_code == 200
    assert "scanner/waveshare/" not in r.text
    assert "ready as-is" in r.text.lower()


def test_unknown_model_falls_back_to_default(client):
    r = client.get("/ui/scanner-setup?model=does-not-exist")
    assert r.status_code == 200
    # Falls back to the recommended sequence rather than 404ing.
    assert "scanner/waveshare/sensing-mode.png" in r.text


def test_scanning_pane_exposes_launch_button_and_qr(client, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "server")
    with patch.object(type(settings), "is_configured", lambda self: True), \
         patch("app.routers.setup.is_raspberry_pi", return_value=False), \
         patch("app.templating.is_raspberry_pi", return_value=False):
        r = client.get("/setup")
    assert r.status_code == 200
    pane = r.text.split('id="pane-scanning"', 1)[1].split('id="pane-', 1)[0]
    # A button that opens the wizard...
    assert 'href="ui/scanner-setup"' in pane
    # ...and a QR that encodes the wizard's LAN URL for a phone.
    assert "ui/qr?url=" in pane
    assert "ui%2Fscanner-setup" in pane or "scanner-setup" in pane


def test_qr_endpoint_returns_image(client):
    r = client.get("/ui/qr", params={"url": "http://192.168.1.50:9284/ui/scanner-setup"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")
    assert b"<svg" in r.content


def test_wizard_service_helpers():
    # The reader picker always resolves to a usable model.
    assert scanner_wizard.get_model(None)["id"] == scanner_wizard.DEFAULT_MODEL_ID
    assert scanner_wizard.get_model("generic")["id"] == "generic"
    # scanner_type steers the default reader where it can.
    assert scanner_wizard.default_model_for("usb") == "generic"
    assert scanner_wizard.default_model_for("") == scanner_wizard.DEFAULT_MODEL_ID
    # Every code path points at an image that exists in the static tree.
    static = _SERVICE / "app" / "static"
    for m in scanner_wizard.MODELS:
        for step in m["steps"]:
            for img in [step.get("image")] + [a["image"] for a in step.get("alternatives", [])]:
                if img:
                    assert (static / img.split("static/", 1)[1]).exists(), img
