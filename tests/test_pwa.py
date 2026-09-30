"""Progressive Web App tests (FoodAssistant-fd3z).

Route-level via TestClient plus on-disk asset checks. Pure logic: no network or
Docker. Confirms the web manifest and service worker are served from the site
ROOT (so the manifest scope is "/" and the worker can control every page), that
both are reachable without auth (an install can start from the login screen),
that the icon set exists, and that base.html wires up the manifest + theme-color.
"""
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SERVICE_DIR = Path(__file__).parent.parent / "service"
_PWA_DIR = _SERVICE_DIR / "app" / "static" / "pwa"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    cwd = os.getcwd()
    os.chdir(_SERVICE_DIR)
    try:
        from app.config import settings

        data_dir = tmp_path_factory.mktemp("data")
        settings.data_dir = str(data_dir)

        from app.main import app

        settings.grocy_base_url = "http://grocy.test"
        settings.grocy_api_key = "test-grocy-key"
        settings.vision_provider = "gemini"
        settings.gemini_api_key = "test-gemini-key"
        settings.auth_required = False
        settings.auth_password = ""
        assert settings.is_configured()

        with TestClient(app) as c:
            yield c
    finally:
        os.chdir(cwd)


def test_manifest_route_is_valid_json(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert "application/manifest+json" in r.headers["content-type"]
    data = json.loads(r.text)  # must parse as JSON
    for key in ("name", "short_name", "description", "start_url", "scope",
                "display", "theme_color", "background_color", "icons"):
        assert key in data, f"manifest missing {key}"
    assert data["name"] == "Pantry Raider"
    assert data["short_name"] == "Pantry Raider"
    assert data["start_url"] == "/"
    assert data["scope"] == "/"
    assert data["display"] == "standalone"
    assert data["theme_color"] == "#F2006E"
    assert data["background_color"] == "#212529"


def test_manifest_icons_cover_required_sizes_and_maskable(client):
    data = client.get("/manifest.webmanifest").json()
    icons = data["icons"]
    sizes = {i["sizes"] for i in icons}
    assert "192x192" in sizes
    assert "512x512" in sizes
    # At least one dedicated maskable icon with safe-zone padding.
    assert any("maskable" in i.get("purpose", "") for i in icons)
    # Every referenced icon src actually exists on disk.
    for i in icons:
        rel = i["src"].lstrip("/")
        assert (_SERVICE_DIR / "app" / rel).is_file(), f"missing {i['src']}"


def test_service_worker_served_at_root_as_javascript(client):
    r = client.get("/sw.js")
    assert r.status_code == 200
    # A JavaScript content type so the browser accepts the worker.
    assert "javascript" in r.headers["content-type"]
    # Served from the root so its default scope covers "/".
    assert r.headers.get("Service-Worker-Allowed") == "/"
    body = r.text
    # Conservative worker markers: network-first navigations, versioned cache.
    assert "addEventListener('fetch'" in body
    assert "networkFirst" in body


def test_pwa_icon_files_exist_on_disk():
    for name in ("icon-192.png", "icon-512.png", "icon-maskable-512.png",
                 "apple-touch-icon-180.png"):
        assert (_PWA_DIR / name).is_file(), f"missing {name}"


def test_base_html_wires_manifest_and_theme_color(client):
    # /ui/ now redirects to the chrome-free Glance home; the manifest wiring lives
    # in the shared base.html chrome, so check a content page (FoodAssistant-gg33).
    page = client.get("/ui/inventory").text
    assert '<link rel="manifest" href="/manifest.webmanifest">' in page
    assert 'name="theme-color"' in page
    # The service worker is registered, guarded to a secure context / localhost.
    assert "serviceWorker" in page
    assert "/sw.js" in page


def test_manifest_and_sw_are_public_pre_auth(client):
    """Both must load without a session so the OS can offer Install on the login
    screen. Assert membership in the auth middleware's public set and that they
    answer 200 with auth enabled and no cookies."""
    import app.main as main
    from app.config import settings

    assert "/manifest.webmanifest" in main._ALWAYS_PUBLIC
    assert "/sw.js" in main._ALWAYS_PUBLIC

    saved = settings.auth_password
    try:
        settings.auth_password = "secret"  # turn auth on
        # A fresh client carries no session cookie.
        with TestClient(main.app) as anon:
            assert anon.get("/manifest.webmanifest").status_code == 200
            assert anon.get("/sw.js").status_code == 200
            # A protected page still bounces (sanity: auth really is on).
            assert anon.get("/ui/inventory", follow_redirects=False).status_code in (302, 303, 307, 401)
    finally:
        settings.auth_password = saved
