"""Buy Me a Coffee support link (FoodAssistant-qg11).

The link is the project owner's static URL, a config constant next to the
Amazon affiliate ones (env-overridable, NOT a user setting), surfaced quietly:
the About page, the README, and the docs index. Never in the kiosk nav.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SERVICE = REPO / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings, BUYMEACOFFEE_URL  # noqa: E402

_URL = "https://www.buymeacoffee.com/syracuse3dprinting"


def test_url_is_a_config_constant_not_a_setting():
    assert BUYMEACOFFEE_URL == _URL
    # Deliberately not a per-user setting: it must not be persistable.
    from app.config import _SAVEABLE
    assert not any("buymeacoffee" in f or "coffee" in f for f in _SAVEABLE)
    assert not hasattr(type(settings)(), "buymeacoffee_url")


def test_url_is_env_overridable():
    # Mirrors the Amazon constants: a BUYMEACOFFEE_URL env var overrides the
    # default at startup. Checked in the source rather than by reloading the
    # config module, which would fork the shared settings singleton mid-suite.
    src = (SERVICE / "app" / "config.py").read_text()
    assert '_os.environ.get("BUYMEACOFFEE_URL"' in src


def test_readme_and_docs_index_carry_the_link():
    assert _URL in (REPO / "README.md").read_text()
    assert _URL in (REPO / "docs" / "index.md").read_text()


def test_kiosk_nav_and_base_chrome_stay_clean():
    # The link belongs on the About page only, never nagging from the chrome.
    for name in ("base.html", "start.html"):
        assert "buymeacoffee" not in \
            (SERVICE / "app" / "templates" / name).read_text().lower()


# -- /ui/about render ---------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd(); os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://g", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_about_page_shows_the_support_link(client):
    r = client.get("/ui/about")
    assert r.status_code == 200
    assert _URL in r.text
    assert "Support the project" in r.text
    assert "bi-cup-hot" in r.text


def test_about_page_hides_the_link_when_unset(monkeypatch, client):
    monkeypatch.setattr("app.routers.ui.BUYMEACOFFEE_URL", "", raising=False)
    r = client.get("/ui/about")
    assert r.status_code == 200
    assert "buymeacoffee" not in r.text.lower()
