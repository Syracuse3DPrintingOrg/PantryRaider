"""Cubs on firmware before 0.19 are never offered an over-the-air update
(FoodAssistant-w0ep5, I-21).

Firmware before 0.19 carried ESPHome's http_request update entity pointed at
this server's manifest and installed whatever it named. Its manifest request
carries only ESPHome's own user agent; its /cub/summary poll carries X-Cub-Id
and X-Cub-Version. The server now tells that firmware there is nothing to
install, refuses it the app image, and never asks it to update itself, while
the browser flasher and firmware that declares 0.19 or later work as before.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings, APP_VERSION
from app.database import Base
from app.models.db_models import CubDevice
from app.services import cub as cub_svc

# The Cub registry on a private in-memory database, as tests/test_cub.py does,
# so these tests never touch the app's real one.
_test_engine = create_engine("sqlite:///:memory:",
                             connect_args={"check_same_thread": False},
                             poolclass=StaticPool)
_TestSession = sessionmaker(bind=_test_engine)
Base.metadata.create_all(bind=_test_engine)


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch):
    monkeypatch.setattr(cub_svc, "SessionLocal", _TestSession)
    db = _TestSession()
    db.query(CubDevice).delete()
    db.commit()
    db.close()

ESPHOME_UA = "ESPHome/2026.6.5 (https://esphome.io)"
OLD_CUB = {"User-Agent": ESPHOME_UA}


def _factory(app_body: bytes = b"\xe9payload") -> bytes:
    return b"\x00" * cub_svc.CUB_APP_OFFSET + app_body


# -- how ESPHome 2026.6.5 reads a manifest -------------------------------------
# A transcription of HttpRequestUpdate::update_task and its main-loop defer in
# esphome/components/http_request/update/http_request_update.cpp (ESPHome
# 2026.6.5). The parse needs string name and version, a builds array, and for
# the build whose chipFamily is the device's own variant, an ota object with
# string path and md5; anything else is a parse error, and the error path
# leaves the previous state in place. An empty version then reads as
# NO_UPDATE.

def _esphome_check(manifest: dict, variant: str, previous_state: str) -> str:
    if not isinstance(manifest.get("name"), str) or not isinstance(
            manifest.get("version"), str) or not isinstance(manifest.get("builds"), list):
        return previous_state
    for build in manifest["builds"]:
        if not isinstance(build.get("chipFamily"), str):
            return previous_state
        if build["chipFamily"] == variant:
            ota = build.get("ota")
            if not isinstance(ota, dict):
                return previous_state
            if not isinstance(ota.get("path"), str) or not isinstance(ota.get("md5"), str):
                return previous_state
            if manifest["version"] == "":
                return "NO_UPDATE"
            return "AVAILABLE"
    return previous_state


# -- pure helpers ---------------------------------------------------------------


def test_minimum_version_constant():
    assert cub_svc.CUB_SELF_UPDATE_MIN == "0.19.0"


@pytest.mark.parametrize("ua,expected", [
    (ESPHOME_UA, True),
    ("esphome/2024.1.0", True),
    ("  ESPHome/1.0  ", True),
    ("Mozilla/5.0 (X11; Linux x86_64) Chrome/130", False),
    ("python-httpx/0.27", False),
    ("", False),
    (None, False),
])
def test_is_on_device_updater(ua, expected):
    assert cub_svc.is_on_device_updater(ua) is expected


@pytest.mark.parametrize("version,expected", [
    ("", False), (None, False), ("   ", False),
    ("0.18.21", False), ("0.18.99", False), ("0.9.0", False),
    ("junk", False), ("dev", False),
    ("0.19", True), ("0.19.0", True), ("v0.19.0", True), (" 0.19.3 ", True),
    ("0.20.1", True), ("1.0.0", True),
    # The tree's own default, which a hand build that skips CI reports.
    ("0.18.36", False),
    # A CI build of a pre-release tag.
    ("0.20.0-rc.1", True),
    # Far too long to be a version; int() would refuse it.
    ("1" + "0" * 5000, False), ("0.19." + "9" * 5000, False),
])
def test_firmware_takes_published_ota(version, expected):
    assert cub_svc.firmware_takes_published_ota(version) is expected


def test_an_absurd_declared_version_is_answered_not_crashed(client, no_image_work):
    """The manifest answers the LAN without a key, so whatever it is handed as
    a version has to come back as a normal answer."""
    huge = "1" + "0" * 5000
    for declared in ({"headers": dict(OLD_CUB, **{"X-Cub-Version": huge})},
                     {"headers": OLD_CUB, "params": {"cub_version": huge}}):
        params = dict({"profile": "tdisplay"}, **declared.get("params", {}))
        r = client.get("/cub/firmware/manifest.json", params=params,
                       headers=declared["headers"])
        assert r.status_code == 200
        assert r.json() == cub_svc.no_update_manifest()


def test_no_update_manifest_exact_shape():
    assert cub_svc.no_update_manifest() == {
        "name": "Bandit Cub",
        "version": "",
        "builds": [
            {"chipFamily": "ESP32", "ota": {"path": "", "md5": ""}},
            {"chipFamily": "ESP32-S3", "ota": {"path": "", "md5": ""}},
        ],
    }


def test_no_update_manifest_covers_every_chip_family():
    families = {m["chip_family"] for m in cub_svc.CUB_PROFILES.values()}
    built = [b["chipFamily"] for b in cub_svc.no_update_manifest()["builds"]]
    assert sorted(built) == sorted(families)
    assert len(built) == len(set(built))


@pytest.mark.parametrize("variant", ["ESP32", "ESP32-S3"])
def test_esphome_reads_the_no_update_manifest_as_no_update(variant):
    """Even a Cub that already saw an update (state AVAILABLE) settles back to
    NO_UPDATE, which is what stops it retrying an install."""
    assert _esphome_check(cub_svc.no_update_manifest(), variant, "AVAILABLE") == "NO_UPDATE"


def test_a_manifest_without_the_ota_block_would_keep_a_stale_update():
    """Why the empty ota block is there at all."""
    m = cub_svc.firmware_manifest("tdisplay", APP_VERSION, ota=None)
    assert _esphome_check(m, "ESP32", "AVAILABLE") == "AVAILABLE"


def test_registry_row_says_whether_firmware_takes_network_updates():
    cub_svc.record_cub_heartbeat("cub-old", firmware_version="0.18.21")
    cub_svc.record_cub_heartbeat("cub-new", firmware_version="0.20.1")
    cub_svc.record_cub_heartbeat("cub-none")
    rows = {r["device_id"]: r for r in cub_svc.list_cubs()}
    assert rows["cub-old"]["network_firmware"] is False
    assert rows["cub-new"]["network_firmware"] is True
    assert rows["cub-none"]["network_firmware"] is False


# -- over the app ---------------------------------------------------------------


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
    from app.routers import expiring as expiring_router
    expiring_router._count_items_cache.invalidate()
    with TestClient(app) as c:
        yield c
    expiring_router._count_items_cache.invalidate()


@pytest.fixture
def local_image(tmp_path):
    local = cub_svc.local_override_path(str(tmp_path), "tdisplay")
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(_factory())
    return local


@pytest.fixture
def no_image_work(monkeypatch):
    """Fails the test if the route touches an image or GitHub at all."""
    from app.routers import cub as cub_router

    async def _boom(*a, **k):
        raise AssertionError("no image work or GitHub fetch for this requester")
    monkeypatch.setattr(cub_router, "_ota_bytes", _boom)
    monkeypatch.setattr(cub_router, "_firmware_bytes", _boom)
    monkeypatch.setattr(cub_router, "_fetch_release_firmware", _boom)
    monkeypatch.setattr(cub_router, "_fetch_release_ota", _boom)


@pytest.mark.parametrize("params", [
    {"profile": "tdisplay"},
    {"profile": "tdisplay-s3"},
    {},                        # the pre-retarget source URL had no profile
    {"profile": "nosuchboard"},
])
def test_old_cub_manifest_offers_nothing(client, no_image_work, params):
    r = client.get("/cub/firmware/manifest.json", params=params, headers=OLD_CUB)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert r.json() == cub_svc.no_update_manifest()


@pytest.mark.parametrize("declared", [
    {"headers": {"X-Cub-Version": "0.18.47"}},
    {"headers": {"X-Cub-Version": ""}},
    {"params": {"cub_version": "0.18.9"}},
    {"params": {"cub_version": "banana"}},
])
def test_on_device_updater_below_019_offers_nothing(client, no_image_work, declared):
    headers = dict(OLD_CUB, **declared.get("headers", {}))
    params = dict({"profile": "tdisplay"}, **declared.get("params", {}))
    r = client.get("/cub/firmware/manifest.json", params=params, headers=headers)
    assert r.json() == cub_svc.no_update_manifest()


def test_declared_019_updater_gets_the_ota_block(client, local_image):
    r = client.get("/cub/firmware/manifest.json", params={"profile": "tdisplay"},
                   headers=dict(OLD_CUB, **{"X-Cub-Version": "0.19.2"}))
    body = r.json()
    assert body["version"] == APP_VERSION
    assert body["builds"][0]["ota"] == cub_svc.firmware_ota_block("tdisplay", _factory())

    q = client.get("/cub/firmware/manifest.json",
                   params={"profile": "tdisplay", "cub_version": "0.20.1"},
                   headers=OLD_CUB).json()
    assert q["builds"][0]["ota"] == body["builds"][0]["ota"]


def test_browser_flasher_gets_the_manifest_without_ota(client, no_image_work):
    """ESP Web Tools reads parts, never ota, so the browser gets no ota block
    and the route does no image work to build one."""
    r = client.get("/cub/firmware/manifest.json", params={"profile": "tdisplay"},
                   headers={"User-Agent": "Mozilla/5.0 Chrome/130"})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    build = r.json()["builds"][0]
    assert build["chipFamily"] == "ESP32"
    assert build["parts"] == [{"path": "tdisplay.bin", "offset": 0}]
    assert "ota" not in build
    assert client.get("/cub/firmware/manifest.json",
                      params={"profile": "bogus"}).status_code == 404


def test_old_cub_is_refused_the_app_image(client, local_image):
    r = client.get("/cub/firmware/tdisplay.ota.bin", headers=OLD_CUB)
    assert r.status_code == 404
    assert r.json()["detail"] == (
        "No over-the-air update is offered to this Cub. "
        "Flash it from the Bandit Cubs page.")
    r = client.get("/cub/firmware/tdisplay.ota.bin",
                   headers=dict(OLD_CUB, **{"X-Cub-Version": "0.18.47"}))
    assert r.status_code == 404


def test_declared_019_updater_and_browser_still_get_the_app_image(client, local_image):
    r = client.get("/cub/firmware/tdisplay.ota.bin",
                   headers=dict(OLD_CUB, **{"X-Cub-Version": "0.19.0"}))
    assert r.status_code == 200
    assert r.content == b"\xe9payload"
    assert client.get("/cub/firmware/tdisplay.ota.bin").content == b"\xe9payload"


def test_factory_image_is_unchanged_for_every_requester(client, local_image):
    for headers in (OLD_CUB, {}):
        r = client.get("/cub/firmware/tdisplay.bin", headers=headers)
        assert r.status_code == 200
        assert r.content == _factory()


# -- /cub/summary ---------------------------------------------------------------


def _stub_backends(monkeypatch):
    from app.routers import cub as cub_router

    async def _items():
        return [], True
    monkeypatch.setattr(cub_router, "_expiring_items", _items)
    monkeypatch.setattr(cub_router, "_timers", lambda: [])
    monkeypatch.setattr(cub_router, "_gadget_devices", lambda: [])
    monkeypatch.setattr(cub_router, "_hygro_devices", lambda: [])
    monkeypatch.setattr(cub_router, "_alarms", lambda: [])
    monkeypatch.setattr(cub_router, "_counts",
                        lambda: {"pending": 0, "action_items": 0})


def _auto(client, headers):
    return client.get("/cub/summary", headers=headers).json()["settings"]["auto_update"]


@pytest.mark.parametrize("headers", [
    {"X-Cub-Id": "cub-old", "X-Cub-Version": "0.18.21"},
    {"X-Cub-Id": "cub-old", "X-Cub-Version": "0.18.99"},
    {"X-Cub-Id": "cub-old"},
    {"X-Cub-Id": "cub-old", "X-Cub-Version": "custom-build"},
])
def test_old_cub_is_never_told_to_update_itself(client, monkeypatch, headers):
    _stub_backends(monkeypatch)
    monkeypatch.setattr(settings, "cub_auto_update", True, raising=False)
    cub_svc.record_cub_heartbeat("cub-old")
    cub_svc.set_cub_overrides("cub-old", {"auto_update": True})

    assert _auto(client, headers) is False
    # Decided per response: the fleet switch and the Cub's own override are
    # exactly as the user left them.
    assert settings.cub_auto_update is True
    row = next(r for r in cub_svc.list_cubs() if r["device_id"] == "cub-old")
    assert row["overrides"] == {"auto_update": True}


def test_new_cub_follows_the_settings(client, monkeypatch):
    _stub_backends(monkeypatch)
    monkeypatch.setattr(settings, "cub_auto_update", True, raising=False)
    new = {"X-Cub-Id": "cub-new", "X-Cub-Version": "0.19.0"}
    assert _auto(client, new) is True
    cub_svc.set_cub_overrides("cub-new", {"auto_update": False})
    assert _auto(client, new) is False
    monkeypatch.setattr(settings, "cub_auto_update", False, raising=False)
    cub_svc.set_cub_overrides("cub-new", {})
    assert _auto(client, new) is False


def test_a_poll_without_a_cub_id_keeps_the_global_value(client, monkeypatch):
    """curl testing and anything that is not a Cub see the fleet setting."""
    _stub_backends(monkeypatch)
    monkeypatch.setattr(settings, "cub_auto_update", True, raising=False)
    assert _auto(client, {}) is True
