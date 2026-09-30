"""Access-core hardening for the 0.20.x release.

- Only the Cub firmware manifest answers the local network without a key; the
  images need a key or a session like everything else.
- The proxy path recorder lives under /admin, so a viewer cannot read it.
- Blocked custom tabs are reported once at startup, and a failure there never
  stops the app from starting.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import settings
from app.passwords import hash_secret

SERVICE = Path(__file__).resolve().parents[1] / "service"

ESPHOME_UA = "ESPHome/2026.6.5 (https://esphome.io)"


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        # "testclient" is not loopback, so the local-screen trust stays out of it.
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _password_install(monkeypatch, viewer=""):
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "api_key", "", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", [], raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret("hunter2"), raising=False)
    monkeypatch.setattr(settings, "viewer_password",
                        hash_secret(viewer) if viewer else "", raising=False)


def _no_release(monkeypatch):
    from app.routers import cub as cub_router

    async def _none(*a, **k):
        return None, "no_release_asset"

    async def _none_ota(*a, **k):
        return None
    monkeypatch.setattr(cub_router, "_fetch_release_firmware", _none)
    monkeypatch.setattr(cub_router, "_fetch_release_ota", _none_ota)


# -- the Cub bypass -------------------------------------------------------------


def test_lan_bypass_covers_only_the_manifest(client, monkeypatch):
    _password_install(monkeypatch)
    _no_release(monkeypatch)
    monkeypatch.setattr("app.main.pairing_svc.is_local_network_request",
                        lambda request: True)
    local = Path(settings.data_dir) / "cub-firmware" / "tdisplay.factory.bin"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(b"\x00" * 0x10000 + b"\xe9payload")

    # A browser on the LAN and an old Cub's update check both still read it.
    r = client.get("/cub/firmware/manifest.json", params={"profile": "tdisplay"})
    assert r.status_code == 200
    assert r.json()["builds"][0]["chipFamily"] == "ESP32"
    r = client.get("/cub/firmware/manifest.json", params={"profile": "tdisplay"},
                   headers={"User-Agent": ESPHOME_UA})
    assert r.status_code == 200
    assert r.json()["version"] == ""

    # The images and the status are no longer open to the LAN.
    assert client.get("/cub/firmware/tdisplay.ota.bin").status_code == 401
    assert client.get("/cub/firmware/tdisplay.ota.bin",
                      headers={"User-Agent": ESPHOME_UA,
                               "X-Cub-Version": "0.19.0"}).status_code == 401
    assert client.get("/cub/firmware/tdisplay.bin").status_code == 401
    assert client.get("/cub/firmware/status").status_code == 401
    # Only a GET of the exact path: a lookalike or another method is not let in.
    assert client.get("/cub/firmware/manifest.json.bak").status_code == 401
    assert client.post("/cub/firmware/manifest.json").status_code == 401
    assert client.get("/cub/summary").status_code == 401


def test_manifest_off_the_lan_still_needs_a_key(client, monkeypatch):
    _password_install(monkeypatch)
    _no_release(monkeypatch)
    monkeypatch.setattr("app.main.pairing_svc.is_local_network_request",
                        lambda request: False)
    assert client.get("/cub/firmware/manifest.json",
                      params={"profile": "tdisplay"}).status_code == 401


def test_browser_flasher_with_a_session_still_gets_every_image(client, monkeypatch):
    """The Bandit Cubs page is opened from a logged-in browser, so the narrower
    bypass costs the flasher nothing."""
    _password_install(monkeypatch)
    _no_release(monkeypatch)
    local = Path(settings.data_dir) / "cub-firmware" / "tdisplay.factory.bin"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(b"\x00" * 0x10000 + b"\xe9payload")
    client.post("/ui/login", data={"password": "hunter2"}, follow_redirects=False)
    assert client.get("/cub/firmware/tdisplay.bin").status_code == 200
    assert client.get("/cub/firmware/tdisplay.ota.bin").status_code == 200
    assert client.get("/cub/firmware/status").status_code == 200


def test_bypass_comment_records_why_the_images_are_gated():
    src = (SERVICE / "app" / "main.py").read_text()
    assert 'request.url.path == "/cub/firmware/manifest.json"' in src
    assert 'startswith("/cub/firmware/")' not in src
    assert "firmware 0.19 and later never downloads" in " ".join(src.split())


# -- the proxy path recorder ----------------------------------------------------


def test_proxy_paths_route_is_mounted_under_admin(client, monkeypatch):
    _password_install(monkeypatch)
    client.post("/ui/login", data={"password": "hunter2"}, follow_redirects=False)
    assert client.get("/admin/proxy-paths").status_code == 200


def test_viewer_cannot_read_proxy_paths(client, monkeypatch):
    _password_install(monkeypatch, viewer="kitchen")
    client.post("/ui/login", data={"password": "kitchen"}, follow_redirects=False)
    assert client.get("/admin/proxy-paths").status_code == 403


def test_anonymous_cannot_read_proxy_paths(client, monkeypatch):
    _password_install(monkeypatch)
    assert client.get("/admin/proxy-paths").status_code == 401


# -- blocked custom tabs are reported once at startup ---------------------------


def _start_app(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr("app.hardware.is_raspberry_pi", lambda: False)
    cwd = os.getcwd()
    os.chdir(SERVICE)
    try:
        with TestClient(app) as c:
            return c.get("/health").status_code
    finally:
        os.chdir(cwd)


def test_startup_reports_blocked_tabs_once(monkeypatch, tmp_path):
    from app.services import url_safety
    calls = []

    def fake_report(db):
        calls.append(db)
        return 0
    monkeypatch.setattr(url_safety, "report_blocked_tabs", fake_report)
    assert _start_app(monkeypatch, tmp_path) == 200
    assert len(calls) == 1
    assert calls[0] is not None


def test_a_failing_report_never_stops_startup(monkeypatch, tmp_path):
    from app.services import url_safety

    def boom(db):
        raise RuntimeError("nav config unreadable")
    monkeypatch.setattr(url_safety, "report_blocked_tabs", boom)
    assert _start_app(monkeypatch, tmp_path) == 200


# -- the Zebra driver ships in the app image ------------------------------------


def test_app_image_carries_the_zebra_driver_where_printing_looks():
    """lpadmin -P reads the PPD in the app container, so the file has to be in
    the app image at the exact path services/printing.py hands it."""
    from app.services.printing import PPD_DRIVERS
    root = SERVICE.parent
    dockerfile = (SERVICE / "Dockerfile").read_text()
    ppd = PPD_DRIVERS["zebra-zpl"]
    assert f"COPY docker/cups/zebra-zpl.ppd {ppd}" in dockerfile
    assert (root / "docker" / "cups" / "zebra-zpl.ppd").is_file()
    # The build context is the repo root; docker/ must not be ignored.
    ignored = [ln.strip().rstrip("/") for ln in
               (root / ".dockerignore").read_text().splitlines()
               if ln.strip() and not ln.lstrip().startswith("#")]
    assert not any(p in ("docker", "docker/cups", "**/*.ppd", "*.ppd") for p in ignored)
