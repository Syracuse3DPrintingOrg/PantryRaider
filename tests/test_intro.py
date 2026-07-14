"""Kiosk boot intro animation (FoodAssistant-a8xy)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402


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


def test_intro_js_gates_and_hooks():
    js = (SERVICE / "app" / "static" / "js" / "intro.js").read_text()
    # Kiosk-only, once per boot: sessionStorage survives navigation but not a
    # kiosk restart, so this flag is the once-per-boot gate.
    assert "kioskMode" in js
    assert "kioskIntroShown" in js
    # Accessibility: reduced motion drops the scale, fade only.
    assert "prefers-reduced-motion" in js
    # The skipping tap is swallowed so it never presses what is underneath
    # (same trick as screensaver.js).
    assert "stopPropagation" in js
    # The script removes the templates' pre-paint blackout guard.
    assert "intro-pending" in js
    assert "intro-blackout" in js
    # Same brand mark the screensaver uses; no new assets.
    assert "static/icons/logo-mark.png" in js


def test_intro_wired_on_base_pages(client, monkeypatch):
    # Pages extending base.html carry the blackout guard and the script.
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/ui/timers")
        assert r.status_code == 200
        assert "static/js/intro.js" in r.text
        assert "intro-pending" in r.text     # the pre-paint blackout guard
        assert "kioskIntroShown" in r.text   # the once-per-boot gate


def test_intro_wired_on_standalone_pages(client):
    # The standalone kiosk-capable pages do not extend base.html, so each
    # includes the shared _intro.html partial itself.
    with patch.object(type(settings), "is_configured", lambda self: True):
        for path in ["/setup", "/ui/start"]:
            r = client.get(path)
            assert r.status_code == 200, path
            assert "static/js/intro.js" in r.text, path
            assert "intro-pending" in r.text, path
