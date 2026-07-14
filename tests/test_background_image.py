"""Background image feature (FoodAssistant-e2t6).

Covers the upload/serve/clear endpoints, the opacity clamp and URL sanitization
in /save, and that base.html paints the fixed background layer when set.
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

from app.config import settings  # noqa: E402

# A valid 1x1 PNG.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app

    cwd = os.getcwd()
    os.chdir(_SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(settings, "background_image_url", "")
    monkeypatch.setattr(settings, "background_opacity", 40)
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _configured():
    return patch.object(type(settings), "is_configured", lambda self: True)


def test_upload_serve_and_clear(client, tmp_path):
    with _configured():
        r = client.post("/setup/background", files={"file": ("bg.png", _PNG, "image/png")})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert settings.background_image_url.startswith("setup/background/image?v=")
    assert (tmp_path / "background.png").exists()

    img = client.get("/setup/background/image")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    assert img.content == _PNG

    with _configured():
        c = client.post("/setup/background/clear", json={})
    assert c.json()["ok"] is True
    assert settings.background_image_url == ""
    assert not (tmp_path / "background.png").exists()
    assert client.get("/setup/background/image").status_code == 404


def test_upload_rejects_non_image(client):
    with _configured():
        r = client.post("/setup/background", files={"file": ("x.txt", b"hi", "text/plain")})
    assert r.json()["ok"] is False


def test_save_clamps_opacity_and_sanitizes_url(client):
    with _configured():
        client.post("/setup/save", json={"background_image_url": "https://ex.com/a.jpg",
                                          "background_opacity": 250})
    assert settings.background_opacity == 100
    assert settings.background_image_url == "https://ex.com/a.jpg"
    # A non-http(s)/non-internal URL is dropped (never written into CSS).
    with _configured():
        client.post("/setup/save", json={"background_image_url": "javascript:alert(1)"})
    assert settings.background_image_url == "https://ex.com/a.jpg"


def test_base_html_paints_background_when_set(client, monkeypatch):
    monkeypatch.setattr(settings, "background_image_url", "https://ex.com/a.jpg")
    monkeypatch.setattr(settings, "background_opacity", 30)
    monkeypatch.setattr(settings, "grocy_base_url", "http://g")
    monkeypatch.setattr(settings, "grocy_api_key", "k")
    with _configured():
        html = client.get("/ui/about").text
    assert "body::before" in html
    assert "https://ex.com/a.jpg" in html
    assert "opacity: 0.3" in html


def test_default_ghosted_brand_watermark_when_unset(client, monkeypatch):
    # With no user background image, the app paints a faint ghosted brand mark
    # (the raccoon) as the default background, not the user-image layer.
    monkeypatch.setattr(settings, "background_image_url", "")
    monkeypatch.setattr(settings, "grocy_base_url", "http://g")
    monkeypatch.setattr(settings, "grocy_api_key", "k")
    with _configured():
        html = client.get("/ui/about").text
    assert "body::before" in html                 # the watermark layer
    assert "logo-mark.png" in html                # is the brand mark
    assert "setup/background/image" not in html   # not a user-uploaded image
