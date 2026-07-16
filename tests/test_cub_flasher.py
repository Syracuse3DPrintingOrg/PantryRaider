"""Bandit Cub flasher page + firmware proxy tests (FoodAssistant-fpde).

Covers the ESP Web Tools manifest shape (per profile, incl. the chipFamily
mapping), the firmware .bin source precedence (local override, then cache,
then a pinned release fetch), the calm not-published 404, the readiness
status endpoint, and the /ui/cubs page itself (all three profile cards, the
download/command fallbacks that work without Web Serial, the self-hosted ESP
Web Tools script rather than a CDN, and that the page is behind auth). No
network: the release fetch is monkeypatched everywhere.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings, APP_VERSION  # noqa: E402
from app.services import cub as cub_svc  # noqa: E402
from app.routers import cub as cub_router  # noqa: E402


# ---------------------------------------------------------------------------
# pure: manifest / naming / paths
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("profile,chip", [
    ("tdisplay", "ESP32"),
    ("tdisplay-s3", "ESP32-S3"),
    ("touch7", "ESP32-S3"),
])
def test_manifest_shape_and_chip_family(profile, chip):
    m = cub_svc.firmware_manifest(profile, "1.2.3")
    assert m["version"] == "1.2.3"
    assert m["new_install_prompt_erase"] is True
    assert len(m["builds"]) == 1
    build = m["builds"][0]
    assert build["chipFamily"] == chip
    # Part path is same-origin-relative to the manifest URL, offset 0.
    assert build["parts"] == [{"path": f"{profile}.bin", "offset": 0}]


def test_manifest_unknown_profile_is_none():
    assert cub_svc.firmware_manifest("nope", APP_VERSION) is None


def test_asset_name_and_release_url_are_pinned():
    assert cub_svc.firmware_asset_name("touch7", "0.9.0") == "bandit-cub-touch7-0.9.0.factory.bin"
    url = cub_svc.release_download_url("Owner/Repo", "0.9.0", "touch7")
    assert url == ("https://github.com/Owner/Repo/releases/download/"
                   "v0.9.0/bandit-cub-touch7-0.9.0.factory.bin")


def test_esptool_command_uses_chip():
    assert "--chip esp32 " in cub_svc.esptool_command("tdisplay", "1.0.0")
    assert "--chip esp32s3 " in cub_svc.esptool_command("touch7", "1.0.0")
    assert cub_svc.esptool_command("nope", "1.0.0") == ""


def test_github_asset_url_host_pinning():
    assert cub_router._is_github_asset_url(
        "https://github.com/x/y/releases/download/v1/a.bin")
    assert cub_router._is_github_asset_url(
        "https://objects.githubusercontent.com/foo")
    assert not cub_router._is_github_asset_url("https://evil.example.com/a.bin")
    assert not cub_router._is_github_asset_url("not a url")


# ---------------------------------------------------------------------------
# endpoints, over the app
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "api_key", "", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", [], raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr("app.hardware.is_raspberry_pi", lambda: False)
    with TestClient(app) as c:
        yield c


def _no_network(monkeypatch):
    async def _boom(*a, **k):
        raise AssertionError("release fetch must not be called")
    monkeypatch.setattr(cub_router, "_fetch_release_firmware", _boom)


def _no_release(monkeypatch):
    """Nothing published for any board, without going near the network."""
    async def _none(*a, **k):
        return None, "no_release_asset"
    monkeypatch.setattr(cub_router, "_fetch_release_firmware", _none)


def test_manifest_endpoint_and_unknown_profile(client, monkeypatch):
    _no_release(monkeypatch)
    m = client.get("/cub/firmware/manifest.json", params={"profile": "tdisplay"}).json()
    assert m["builds"][0]["chipFamily"] == "ESP32"
    # No image to hash means no ota block, and the browser flasher's part is
    # still there: the page works, and a Cub just tries again later.
    assert "ota" not in m["builds"][0]
    assert m["builds"][0]["parts"] == [{"path": "tdisplay.bin", "offset": 0}]
    assert client.get("/cub/firmware/manifest.json",
                      params={"profile": "bogus"}).status_code == 404


def test_bin_local_override_wins_without_network(client, tmp_path, monkeypatch):
    _no_network(monkeypatch)
    fw = cub_svc.firmware_dir(str(tmp_path))
    fw.mkdir(parents=True, exist_ok=True)
    (fw / "tdisplay.factory.bin").write_bytes(b"LOCAL-IMAGE")
    r = client.get("/cub/firmware/tdisplay.bin")
    assert r.status_code == 200
    assert r.content == b"LOCAL-IMAGE"


def test_bin_cache_hit_without_network(client, tmp_path, monkeypatch):
    _no_network(monkeypatch)
    cached = cub_svc.cached_firmware_path(str(tmp_path), "touch7", APP_VERSION)
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"CACHED-IMAGE")
    r = client.get("/cub/firmware/touch7.bin")
    assert r.status_code == 200
    assert r.content == b"CACHED-IMAGE"


def test_bin_fetch_caches_for_next_time(client, tmp_path, monkeypatch):
    calls = {"n": 0}

    async def _fake(profile, version):
        calls["n"] += 1
        return b"FETCHED-IMAGE", None
    monkeypatch.setattr(cub_router, "_fetch_release_firmware", _fake)

    r = client.get("/cub/firmware/tdisplay-s3.bin")
    assert r.status_code == 200
    assert r.content == b"FETCHED-IMAGE"
    # The fetched bytes are cached under version+profile, so the next flash is
    # local: a second request never fetches again.
    cached = cub_svc.cached_firmware_path(str(tmp_path), "tdisplay-s3", APP_VERSION)
    assert cached.exists() and cached.read_bytes() == b"FETCHED-IMAGE"
    r2 = client.get("/cub/firmware/tdisplay-s3.bin")
    assert r2.status_code == 200 and r2.content == b"FETCHED-IMAGE"
    assert calls["n"] == 1


def test_bin_calm_404_when_nothing_published(client, monkeypatch):
    async def _none(profile, version):
        return None, "no_release_asset"
    monkeypatch.setattr(cub_router, "_fetch_release_firmware", _none)
    r = client.get("/cub/firmware/touch7.bin")
    assert r.status_code == 404
    body = r.json()
    assert body["profile"] == "touch7"
    assert "not been published" in body["detail"]


def test_bin_unknown_profile_404(client):
    assert client.get("/cub/firmware/bogus.bin").status_code == 404


def test_status_reports_local_override_available(client, tmp_path, monkeypatch):
    # No network: a board with a dropped-in image is available; the others fall
    # to the release HEAD, which we stub to "not published".
    async def _no_release(profile, version):
        return False
    monkeypatch.setattr(cub_router, "_release_asset_available", _no_release)
    fw = cub_svc.firmware_dir(str(tmp_path))
    fw.mkdir(parents=True, exist_ok=True)
    (fw / "tdisplay.factory.bin").write_bytes(b"x")
    data = client.get("/cub/firmware/status").json()
    assert data["version"] == APP_VERSION
    assert data["profiles"]["tdisplay"]["available"] is True
    assert data["profiles"]["touch7"]["available"] is False
    # Each profile carries its board/chip/command for the page to render.
    assert data["profiles"]["touch7"]["chip_family"] == "ESP32-S3"
    assert "esptool.py" in data["profiles"]["touch7"]["esptool"]


# ---------------------------------------------------------------------------
# the /ui/cubs page
# ---------------------------------------------------------------------------

def _render_cubs(client):
    # The Jinja loader resolves templates relative to the working directory, so
    # render from service/ like the setup-pane test does.
    cwd = os.getcwd()
    os.chdir(SERVICE)
    try:
        return client.get("/ui/cubs")
    finally:
        os.chdir(cwd)


def test_page_renders_all_three_cards_and_affordances(client):
    html = _render_cubs(client).text
    # All three profile cards.
    for profile in ("tdisplay", "tdisplay-s3", "touch7"):
        assert f'data-profile="{profile}"' in html
        assert f'manifest="/cub/firmware/manifest.json?profile={profile}"' in html
        assert f'href="/cub/firmware/{profile}.bin"' in html
    # Fallbacks that work without Web Serial: an esptool command and the
    # browser-based alternative.
    assert "esptool.py --chip esp32 " in html
    assert "web.esphome.io" in html
    # The YAML escape hatch points at the ESPHome project.
    assert "/tree/main/esphome" in html


def test_page_uses_self_hosted_esp_web_tools_not_a_cdn(client):
    html = _render_cubs(client).text
    assert "static/js/vendor/esp-web-tools/install-button.js" in html
    for cdn in ("unpkg.com", "jsdelivr", "cdn.jsdelivr", "esm.sh"):
        assert cdn not in html


def test_page_requires_auth(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_password", "hunter2", raising=False)
    monkeypatch.setattr(settings, "api_key", "test-key", raising=False)
    # A browser with no session is bounced to the login page (a redirect), never
    # served the flasher.
    r = client.get("/ui/cubs", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "/ui/login" in r.headers.get("location", "")


def test_page_firmware_urls_are_root_relative(client):
    """The page lives at /ui/cubs, so a relative "cub/firmware/..." URL would
    resolve to /ui/cub/firmware/... and 404 (the bug behind "Install
    undefined" on the first live flash). Every firmware reference must be
    root-relative."""
    html = _render_cubs(client).text
    assert "/cub/firmware/manifest.json?profile=" in html
    assert "/cub/firmware/status" in html
    # No relative firmware references anywhere: every mention of the firmware
    # path must be immediately preceded by / or a quote+slash.
    import re
    for m in re.finditer(r"""["'(](cub/firmware/)""", html):
        raise AssertionError(f"relative firmware URL on the page: ...{html[m.start()-20:m.end()+30]}...")
