"""The Start page's Glance landing follows the selected theme.

The landing was styled in the pantryraider.app website's palette (a near-black
stage, pink and purple glow) with those colors written straight into the page,
so picking Light, Solarized, Synthwave or a custom theme changed every other
page but left the main splash page on the brand look. The brand theme keeps
that look; every other theme marks the landing glance-themed, which maps its
colors onto the theme's own Bootstrap variables.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402

_START = SERVICE / "app/templates/start.html"


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
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _landing_classes(html: str) -> str:
    m = re.search(r'class="(start-wrap[^"]*)"', html)
    assert m, "the Start page lost its start-wrap container"
    return m.group(1)


def _get_start(client):
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/ui/start")
    assert r.status_code == 200
    return r.text


def test_brand_theme_keeps_the_website_look(client, monkeypatch):
    monkeypatch.setattr(settings, "ui_theme", "pantryraider", raising=False)
    html = _get_start(client)
    classes = _landing_classes(html)
    assert "glance-landing" in classes
    assert "glance-themed" not in classes
    assert "#ff4d9b" in html, "the brand theme's pink and purple tile glow is gone"


@pytest.mark.parametrize("theme", ["light", "solarized", "synthwave", "forest", "flatly"])
def test_other_themes_mark_the_landing_themed(client, monkeypatch, theme):
    monkeypatch.setattr(settings, "ui_theme", theme, raising=False)
    html = _get_start(client)
    assert "glance-themed" in _landing_classes(html)
    # The tiles glow in the theme's accents, not the brand's fixed pinks.
    tiles = re.findall(r'class="glance-card"[^>]*style="--neon: ([^"]+)"', html)
    assert tiles, "no Glance tiles rendered"
    assert all("var(--pr-accent" in t or "color-mix" in t for t in tiles)


def test_light_theme_softens_the_shadows(client, monkeypatch):
    monkeypatch.setattr(settings, "ui_theme", "light", raising=False)
    html = _get_start(client)
    block = re.search(r"\.glance-landing\.glance-themed \{([^}]*)\}", html).group(1)
    assert "--pr-shadow: rgba(0, 0, 0, 0.1)" in block


def test_glance_colors_come_from_tokens_not_literals():
    """No fixed white glass or dark chip colors left in the Glance rules.

    Literal rgba(255, 255, 255, ...) glass is invisible on a light theme, and
    a fixed dark chip turns the pills into black blobs there. Everything past
    the token definitions has to go through the --pr-* variables.
    """
    src = _START.read_text(encoding="utf-8")
    start = src.index(".glance-landing::before {")
    end = src.index("</style>", start)
    rules = src[start:end]
    assert "rgba(255, 255, 255" not in rules
    assert "rgba(22, 22, 29" not in rules
    assert "#b5179e" not in rules
