"""The public demo's theme picker.

On the demo a visitor can try each theme from a small tab on the right edge.
The pick lives in the pr_demo_theme cookie and only changes how pages render
for that browser. A normal install shows no picker and ignores the cookie, and
the cookie can only name a built-in theme.
"""
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
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "test-key", raising=False)
    monkeypatch.setattr(settings, "start_page_enabled", True, raising=False)
    monkeypatch.setattr(settings, "start_page_mode", "glance", raising=False)
    monkeypatch.setattr(settings, "ui_theme", "pantryraider", raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _start(client, cookie=None):
    if cookie is not None:
        client.cookies.set("pr_demo_theme", cookie)
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/ui/start")
    assert r.status_code == 200
    return r.text


def test_normal_install_has_no_picker_and_ignores_the_cookie(client, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", False, raising=False)
    html = _start(client, "solarized")
    assert 'id="prDemoTheme"' not in html
    assert "solarized.css" not in html
    assert 'data-bs-theme="dark"' in html


def test_demo_shows_the_picker_with_every_builtin_theme(client, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True, raising=False)
    html = _start(client)
    assert 'id="prDemoTheme"' in html
    assert 'value="synthwave"' in html and 'value="ios-light"' in html
    assert 'value="custom"' not in html
    assert 'value="pantryraider" selected' in html


def test_demo_cookie_changes_the_theme_for_that_browser(client, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True, raising=False)
    html = _start(client, "solarized")
    assert "static/vendor/themes/solarized.css" in html
    assert 'data-bs-theme="light"' in html
    assert 'value="solarized" selected' in html
    assert "glance-landing glance-themed" in html
    # The saved setting is untouched.
    assert settings.ui_theme == "pantryraider"


@pytest.mark.parametrize("bad", ["custom", "custom:abc", "../../etc/passwd", "nope"])
def test_demo_cookie_only_names_a_builtin_theme(client, monkeypatch, bad):
    monkeypatch.setattr(settings, "demo_mode", True, raising=False)
    html = _start(client, bad)
    assert "static/vendor/themes/pantryraider.css" in html
    assert "glance-landing glance-themed" not in html


def test_picker_shows_on_regular_pages_too(client, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True, raising=False)
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/ui/about")
    assert r.status_code == 200
    assert 'id="prDemoTheme"' in r.text
