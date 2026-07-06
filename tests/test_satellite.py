"""Satellite config-federation tests.

Exercise the server-side config endpoint (what a main server hands out) and the
pull-side apply logic (how a satellite mirrors it), without real network or a
second running instance. Pure logic + FastAPI TestClient.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings, SATELLITE_PULL_FIELDS  # noqa: E402


# -- server side: GET /api/config/satellite ----------------------------------

@pytest.fixture
def client():
    # Templates load from the relative path "app/templates", so run from service/.
    from fastapi.testclient import TestClient
    from app.main import app
    cwd = os.getcwd()
    os.chdir(SERVICE)
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_config_endpoint_refuses_without_server_api_key(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "")
    r = client.get("/api/config/satellite")
    assert r.status_code == 503


def test_config_endpoint_rejects_bad_key(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "secret-key")
    monkeypatch.setattr(settings, "auth_password", "")  # avoid auth middleware
    r = client.get("/api/config/satellite", headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


def test_config_endpoint_serves_shareable_fields(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "secret-key")
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(settings, "grocy_base_url", "http://server:9383")
    monkeypatch.setattr(settings, "grocy_api_key", "grocy-key")
    r = client.get("/api/config/satellite", headers={"X-API-Key": "secret-key"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # Every shareable field is present; no device-local secret leaks.
    assert set(body["config"].keys()) == set(SATELLITE_PULL_FIELDS)
    assert body["config"]["grocy_base_url"] == "http://server:9383"
    assert "secret_key" not in body["config"]
    assert "auth_password" not in body["config"]
    assert "api_key" not in body["config"]
    assert isinstance(body["expiry_defaults"], list)
    # The server advertises its hostname so a bare-IP satellite can learn the
    # mDNS fallback name (FoodAssistant-k9a8).
    assert "server_hostname" in body and isinstance(body["server_hostname"], str)


# -- pull side: apply config onto live settings ------------------------------

def test_apply_config_sets_only_shareable_fields(monkeypatch, tmp_path):
    from app.services.satellite import _apply_config
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    applied = _apply_config({
        "grocy_base_url": "http://server:9383",
        "gemini_api_key": "pulled-key",
        "secret_key": "SHOULD-NOT-APPLY",  # not in SATELLITE_PULL_FIELDS
    })
    assert "grocy_base_url" in applied
    assert "gemini_api_key" in applied
    assert "secret_key" not in applied
    assert settings.grocy_base_url == "http://server:9383"
    assert settings.gemini_api_key == "pulled-key"
    assert getattr(settings, "secret_key") != "SHOULD-NOT-APPLY"
    assert settings.server_sourced_fields >= {"grocy_base_url", "gemini_api_key"}


def test_apply_config_inherits_timezone(monkeypatch, tmp_path):
    # A Pi Remote inherits the fleet timezone from the main server (it is a
    # SATELLITE_PULL_FIELD), not a per-device option (FoodAssistant-amp0).
    from app.services.satellite import _apply_config
    from app.config import SATELLITE_PULL_FIELDS
    assert "timezone" in SATELLITE_PULL_FIELDS
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "timezone", "", raising=False)
    applied = _apply_config({"timezone": "America/Chicago"})
    assert "timezone" in applied
    assert settings.timezone == "America/Chicago"


def test_push_timezone_noop_off_pi(monkeypatch):
    # Off a Pi (no host bridge) the push is a safe no-op, never raising.
    from app.services import satellite as sat
    monkeypatch.setattr("app.hardware.is_raspberry_pi", lambda: False)
    assert sat._push_timezone("America/Chicago") is False
    assert sat._push_timezone("") is False


def test_apply_config_persists_pulled_fields(monkeypatch, tmp_path):
    # Pulled config must hit settings.json so it survives a restart and is shared
    # across worker processes (the camera-not-showing-on-pi_remote bug).
    import json
    from app.services.satellite import _apply_config
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "streamdeck_cameras", [], raising=False)
    cams = [{"name": "Doorbell", "stream_url": "http://x/s", "snapshot_url": "http://x/snap"}]
    _apply_config({"streamdeck_cameras": cams, "streamdeck_ha_base_url": "http://ha:8123"})
    saved = json.loads((tmp_path / "settings.json").read_text())
    assert saved["streamdeck_cameras"] == cams
    assert saved["streamdeck_ha_base_url"] == "http://ha:8123"


def test_sync_noops_when_not_satellite(monkeypatch):
    from app.services.satellite import sync_from_upstream
    monkeypatch.setattr(settings, "deployment_mode", "server")
    out = sync_from_upstream()
    assert out["ok"] is False
    assert out["error"] == "not a satellite"


def test_sync_requires_url_and_key(monkeypatch):
    from app.services.satellite import sync_from_upstream
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(settings, "remote_server_url", "")
    monkeypatch.setattr(settings, "upstream_api_key", "")
    out = sync_from_upstream()
    assert out["ok"] is False
    assert "missing" in out["error"]


# -- mode semantics ----------------------------------------------------------

def test_satellite_is_configured_needs_url_and_key(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "remote_server_url", "http://server:9284")
    monkeypatch.setattr(settings, "upstream_api_key", "")
    assert settings.is_configured() is False
    monkeypatch.setattr(settings, "upstream_api_key", "k")
    assert settings.is_configured() is True


def test_satellite_features_show_backend_panes(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    f = settings.features()
    assert f["satellite"] is True
    assert f["manages_stack"] is False
    assert f["ai"] is True


# -- integration: sync_from_upstream against a mocked HTTP layer -------------
#
# These drive the real sync_from_upstream code path (header build, status
# handling, _apply_config, defaults, provider invalidation) but swap the
# module-level httpx.get for a fake so nothing touches the network. The DB-
# backed _apply_defaults and the provider cache reset_providers are also
# patched so the test stays pure logic, while still letting us assert that
# sync invalidates providers on a successful pull.

def _httpx_connect_error():
    """A representative httpx network failure for the unreachable-server case."""
    import httpx
    return httpx.ConnectError("connection refused")


class _FakeResponse:
    """Minimal stand-in for httpx.Response: just what sync_from_upstream reads."""

    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


@pytest.fixture
def satellite_mode(monkeypatch, tmp_path):
    """Put settings into a fully configured satellite state for sync tests.

    Point data_dir at a fresh temp dir so settings.save() (called via
    _record_last_sync during a sync) writes to an empty settings.json instead
    of the real one. Otherwise apply() re-overlays whatever _SAVEABLE fields
    happen to be persisted on disk and clobbers the values the pull just set,
    which made these tests pass only when an earlier test had left the expected
    values in the shared file.
    """
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(settings, "remote_server_url", "http://server:9284")
    monkeypatch.setattr(settings, "upstream_api_key", "upstream-secret")
    monkeypatch.setattr(settings, "device_id", "dev-abc")
    # Start from blanks so we can prove which fields the pull wrote.
    monkeypatch.setattr(settings, "grocy_base_url", "")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    # server_sourced_fields is not a declared pydantic field; the production
    # code sets it via object.__setattr__, so do the same here and restore it.
    prior = getattr(settings, "server_sourced_fields", set())
    object.__setattr__(settings, "server_sourced_fields", set())
    yield
    object.__setattr__(settings, "server_sourced_fields", prior)


def test_sync_happy_path_applies_fields_and_invalidates_providers(satellite_mode):
    from app.services import satellite as sat

    payload = {
        "ok": True,
        "config": {
            "grocy_base_url": "http://server:9383",
            "gemini_api_key": "pulled-key",
        },
        "expiry_defaults": [
            {"category": "dairy", "name_pattern": "milk", "storage_type": "fridge",
             "default_days": 7, "priority": 1},
        ],
        "command": None,
    }

    with patch.object(sat.httpx, "get", return_value=_FakeResponse(200, payload)) as mock_get, \
            patch.object(sat, "_apply_defaults", return_value=1) as mock_defaults, \
            patch("app.dependencies.reset_providers") as mock_reset:
        out = sat.sync_from_upstream()

    assert out["ok"] is True
    assert set(out["applied"]) >= {"grocy_base_url", "gemini_api_key"}
    assert out["defaults"] == 1
    assert out["error"] is None
    assert settings.grocy_base_url == "http://server:9383"
    assert settings.gemini_api_key == "pulled-key"
    assert settings.server_sourced_fields >= {"grocy_base_url", "gemini_api_key"}
    # The pull hit the expected endpoint with the upstream key, and providers
    # were invalidated so freshly pulled keys take effect.
    called_url = mock_get.call_args.args[0]
    assert called_url == "http://server:9284/api/config/satellite"
    headers = mock_get.call_args.kwargs["headers"]
    assert headers["X-API-Key"] == "upstream-secret"
    mock_defaults.assert_called_once()
    mock_reset.assert_called_once()


def test_sync_picks_up_mealie_started_after_setup(satellite_mode):
    """Regression for FoodAssistant-1jbq: a satellite set up while Mealie was
    not yet running must flip to Mealie-available on a later sync, with no
    restart. mealie_configured() is a stateless check of the pulled fields, and
    the periodic sync re-pulls them, so applying a config that now includes
    Mealie is enough."""
    from app.services import satellite as sat

    # Satellite starts with Mealie unconfigured (fixture leaves these blank-ish;
    # force the blank state explicitly so the assertion is unambiguous).
    object.__setattr__(settings, "mealie_base_url", "")
    object.__setattr__(settings, "mealie_api_key", "")
    assert settings.mealie_configured() is False

    # The server has since had Mealie added, so the next pull carries it.
    payload = {
        "ok": True,
        "config": {
            "mealie_base_url": "http://server:9285",
            "mealie_api_key": "mealie-key",
        },
        "expiry_defaults": [],
        "command": None,
    }
    with patch.object(sat.httpx, "get", return_value=_FakeResponse(200, payload)), \
            patch.object(sat, "_apply_defaults", return_value=0), \
            patch("app.dependencies.reset_providers"):
        out = sat.sync_from_upstream()

    assert out["ok"] is True
    assert set(out["applied"]) >= {"mealie_base_url", "mealie_api_key"}
    # Without any restart, Mealie now reads as available.
    assert settings.mealie_configured() is True


def test_sync_unreachable_server_keeps_existing_config(satellite_mode):
    from app.services import satellite as sat

    monkeypatch_value = "http://existing:9383"
    object.__setattr__(settings, "grocy_base_url", monkeypatch_value)

    with patch.object(sat.httpx, "get", side_effect=_httpx_connect_error()) as mock_get, \
            patch.object(sat, "_apply_defaults") as mock_defaults, \
            patch("app.dependencies.reset_providers") as mock_reset:
        out = sat.sync_from_upstream()

    assert out["ok"] is False
    assert "cannot reach server" in out["error"]
    assert out["applied"] == []
    assert out["defaults"] == 0
    # Existing config is untouched and nothing downstream ran.
    assert settings.grocy_base_url == monkeypatch_value
    mock_defaults.assert_not_called()
    mock_reset.assert_not_called()
    assert mock_get.called


def test_sync_bad_api_key_401_handled_gracefully(satellite_mode):
    from app.services import satellite as sat

    resp = _FakeResponse(401, {"detail": "bad api key"})
    with patch.object(sat.httpx, "get", return_value=resp), \
            patch.object(sat, "_apply_defaults") as mock_defaults, \
            patch("app.dependencies.reset_providers") as mock_reset:
        out = sat.sync_from_upstream()

    assert out["ok"] is False
    assert "401" in out["error"]
    assert "bad api key" in out["error"]
    assert out["applied"] == []
    # A rejected pull applies nothing and leaves the provider cache alone.
    assert settings.grocy_base_url == ""
    mock_defaults.assert_not_called()
    mock_reset.assert_not_called()


def test_sync_partial_payload_without_defaults_still_applies_config(satellite_mode):
    from app.services import satellite as sat

    # No expiry_defaults key at all: config should apply, defaults step is a no-op.
    payload = {
        "ok": True,
        "config": {"grocy_base_url": "http://server:9383"},
        "command": None,
    }

    with patch.object(sat.httpx, "get", return_value=_FakeResponse(200, payload)), \
            patch.object(sat, "_apply_defaults", return_value=0) as mock_defaults, \
            patch("app.dependencies.reset_providers") as mock_reset:
        out = sat.sync_from_upstream()

    assert out["ok"] is True
    assert "grocy_base_url" in out["applied"]
    assert out["defaults"] == 0
    assert settings.grocy_base_url == "http://server:9383"
    # _apply_defaults is called with the empty default and must not error.
    mock_defaults.assert_called_once_with([])
    mock_reset.assert_called_once()


# Stream Deck weather sync from the main server (FoodAssistant-bra)
# ----------------------------------------------------------------

def test_streamdeck_weather_fields_are_pulled():
    """The weather location/units must be in the satellite pull set so a
    satellite mirrors the server's Stream Deck weather config."""
    assert "streamdeck_weather_location" in SATELLITE_PULL_FIELDS
    assert "streamdeck_weather_units" in SATELLITE_PULL_FIELDS


def test_streamdeck_visual_style_is_device_local():
    """Key style and icon colour are a per-deck choice, NOT pulled from the
    server, so a satellite can pick its own (e.g. the full-colour emoji set)
    and have it stick instead of being overwritten on the next sync (ys79)."""
    from app.services import satellite as sat

    assert "streamdeck_key_style" not in SATELLITE_PULL_FIELDS
    assert "streamdeck_icon_color" not in SATELLITE_PULL_FIELDS
    assert "streamdeck_key_style" not in sat._STREAMDECK_SYNCED_FIELDS
    assert "streamdeck_icon_color" not in sat._STREAMDECK_SYNCED_FIELDS


def test_merge_streamdeck_settings_overlays_only_weather_and_theme():
    from app.services import satellite as sat

    base = {"rotation": 90, "brightness": 50, "keys": ["a", "b"],
            "weather_location": "old", "weather_units": "c", "theme": "dark"}
    merged = sat._merge_streamdeck_settings(base, "Boston", "f", "synthwave")
    # Weather + theme overlaid, everything else preserved, original not mutated.
    assert merged["weather_location"] == "Boston"
    assert merged["weather_units"] == "f"
    assert merged["theme"] == "synthwave"
    assert merged["rotation"] == 90
    assert merged["keys"] == ["a", "b"]
    assert base["weather_location"] == "old"


def test_custom_keys_are_synced_to_satellites():
    """Custom Stream Deck keys are pulled and pushed to a satellite's deck so a
    button built on the server shows everywhere (FoodAssistant-n0r1)."""
    from app.services import satellite as sat
    assert "streamdeck_key_overrides" in SATELLITE_PULL_FIELDS
    assert "streamdeck_key_overrides" in sat._STREAMDECK_SYNCED_FIELDS
    merged = sat._merge_streamdeck_settings(
        {"rotation": 0}, "B", "f", "dark",
        key_overrides=[{"slot": 3, "type": "shopping_add", "item": "Milk"},
                       "bad-entry"],
    )
    # Only well-formed override dicts survive into the deck config.
    assert merged["key_overrides"] == [{"slot": 3, "type": "shopping_add", "item": "Milk"}]


def test_merge_streamdeck_settings_overlays_ha_and_cameras():
    from app.services import satellite as sat

    base = {"rotation": 0}
    merged = sat._merge_streamdeck_settings(
        base, "Boston", "f", "dark", "clean", "color",
        ha_base_url="http://ha.local:8123",
        ha_token="secret-llat",
        ha_slots=[{"entity_id": "light.kitchen", "service": "light.toggle"}],
        cameras=[{"name": "Door", "stream_url": "http://x/s.m3u8",
                  "snapshot_url": "http://x/snap.jpg", "extra": "drop me"}],
    )
    # HA credentials and key map propagate so a satellite's deck inherits them.
    assert merged["ha_base_url"] == "http://ha.local:8123"
    assert merged["ha_token"] == "secret-llat"
    assert merged["ha_slots"][0]["entity_id"] == "light.kitchen"
    # The deck needs name + snapshot_url + ha_entity; other keys are dropped.
    assert merged["cameras"] == [{"name": "Door", "snapshot_url": "http://x/snap.jpg", "ha_entity": ""}]
    assert base == {"rotation": 0}  # original not mutated


def test_ha_camera_urls_embed_token():
    from app.routers.setup import _ha_camera_urls

    urls = _ha_camera_urls("http://ha.local:8123/", "tok en", "camera.front_door")
    # Trailing slash trimmed, entity + token URL-encoded, both proxy endpoints built.
    assert urls["stream_url"] == (
        "http://ha.local:8123/api/camera_proxy_stream/camera.front_door?token=tok%20en"
    )
    assert urls["snapshot_url"] == (
        "http://ha.local:8123/api/camera_proxy/camera.front_door?token=tok%20en"
    )


def test_push_streamdeck_settings_skipped_off_pi(monkeypatch):
    from app.services import satellite as sat

    # Off a Pi (or with no deck) the push is a no-op and never touches the
    # bridge, so a sync on a server/phone does not error.
    monkeypatch.setattr("app.hardware.is_raspberry_pi", lambda: False)
    called = {"hit": False}

    def _should_not_run(*a, **k):
        called["hit"] = True
        raise AssertionError("bridge must not be called off a Pi")

    monkeypatch.setattr(sat.httpx, "get", _should_not_run)
    monkeypatch.setattr(sat.httpx, "post", _should_not_run)
    assert sat._push_streamdeck_settings() is False
    assert called["hit"] is False


def test_sync_pushes_streamdeck_when_theme_pulled(satellite_mode, monkeypatch):
    """A pulled UI theme change triggers the controller config.toml push so the
    deck recolours to match the server (gxl)."""
    from app.services import satellite as sat

    payload = {
        "ok": True,
        "config": {"ui_theme": "synthwave"},
        "expiry_defaults": [],
        "command": None,
    }
    pushes = []
    monkeypatch.setattr(sat, "_push_streamdeck_settings", lambda *a, **k: pushes.append(True) or True)
    with patch.object(sat.httpx, "get", return_value=_FakeResponse(200, payload)), \
            patch.object(sat, "_apply_defaults", return_value=0), \
            patch("app.dependencies.reset_providers"):
        out = sat.sync_from_upstream()

    assert out["ok"] is True
    assert settings.ui_theme == "synthwave"
    assert pushes == [True]


def test_sync_pushes_weather_when_pulled(satellite_mode, monkeypatch):
    """When a sync applies the weather fields, the controller config.toml push
    runs; when it does not, the push is skipped."""
    from app.services import satellite as sat

    payload = {
        "ok": True,
        "config": {
            "streamdeck_weather_location": "Seattle",
            "streamdeck_weather_units": "c",
        },
        "expiry_defaults": [],
        "command": None,
    }
    pushes = []
    monkeypatch.setattr(sat, "_push_streamdeck_settings", lambda *a, **k: pushes.append(True) or True)
    with patch.object(sat.httpx, "get", return_value=_FakeResponse(200, payload)), \
            patch.object(sat, "_apply_defaults", return_value=0), \
            patch("app.dependencies.reset_providers"):
        out = sat.sync_from_upstream()

    assert out["ok"] is True
    assert settings.streamdeck_weather_location == "Seattle"
    assert settings.streamdeck_weather_units == "c"
    assert pushes == [True]


# -- Stream Deck profile sync (FoodAssistant-aqa) ----------------------------

def test_apply_profiles_mirrors_server_profiles(tmp_path, monkeypatch):
    """_apply_profiles replaces the local profiles table with the server copy."""
    from app.services.satellite import _apply_profiles
    from app.database import SessionLocal
    from app.models.db_models import StreamDeckProfile

    rows = [
        {"name": "kitchen", "deck_size": 15, "key_overrides": [{"slot": 0, "type": "expiring"}], "updated_at": "2026-01-01T00:00:00+00:00"},
        {"name": "office", "deck_size": 6, "key_overrides": [], "updated_at": "2026-01-01T00:00:00+00:00"},
    ]
    count = _apply_profiles(rows)
    assert count == 2
    db = SessionLocal()
    try:
        names = [r.name for r in db.query(StreamDeckProfile).order_by(StreamDeckProfile.name).all()]
    finally:
        db.close()
    assert names == ["kitchen", "office"]


def test_apply_profiles_replaces_on_resync(tmp_path):
    """Calling _apply_profiles twice replaces the previous set."""
    from app.services.satellite import _apply_profiles
    from app.database import SessionLocal
    from app.models.db_models import StreamDeckProfile

    _apply_profiles([
        {"name": "old", "deck_size": 15, "key_overrides": [], "updated_at": "2026-01-01T00:00:00+00:00"},
    ])
    _apply_profiles([
        {"name": "new", "deck_size": 32, "key_overrides": [], "updated_at": "2026-01-02T00:00:00+00:00"},
    ])
    db = SessionLocal()
    try:
        names = [r.name for r in db.query(StreamDeckProfile).all()]
    finally:
        db.close()
    assert names == ["new"]
    assert "old" not in names


def test_sync_mirrors_profiles(satellite_mode, monkeypatch):
    """A successful satellite pull mirrors the profiles list to the local DB."""
    from app.services import satellite as sat

    payload = {
        "ok": True,
        "config": {},
        "expiry_defaults": [],
        "streamdeck_profiles": [
            {"name": "demo", "deck_size": 15, "key_overrides": [], "updated_at": "2026-01-01T00:00:00+00:00"},
        ],
        "command": None,
    }
    with patch.object(sat.httpx, "get", return_value=_FakeResponse(200, payload)), \
            patch.object(sat, "_apply_defaults", return_value=0), \
            patch.object(sat, "_apply_profiles", return_value=1) as mock_ap, \
            patch("app.dependencies.reset_providers"):
        out = sat.sync_from_upstream()

    assert out["ok"] is True
    mock_ap.assert_called_once_with(payload["streamdeck_profiles"])


def test_satellite_config_endpoint_includes_profiles(client, monkeypatch):
    """The /api/config/satellite response includes a streamdeck_profiles list."""
    monkeypatch.setattr(settings, "api_key", "secret-key")
    monkeypatch.setattr(settings, "auth_password", "")
    r = client.get("/api/config/satellite", headers={"X-API-Key": "secret-key"})
    assert r.status_code == 200
    body = r.json()
    assert "streamdeck_profiles" in body
    assert isinstance(body["streamdeck_profiles"], list)


# -- mDNS-resilient sync: IP fallback (FoodAssistant-xwn0) -------------------


def test_swap_host_keeps_scheme_port_path():
    from app.services.satellite import _swap_host
    assert _swap_host(
        "http://server.local:9284/api/config/satellite", "192.168.1.50"
    ) == "http://192.168.1.50:9284/api/config/satellite"


def test_is_ip_literal():
    from app.services.satellite import _is_ip_literal
    assert _is_ip_literal("192.168.1.50") is True
    assert _is_ip_literal("::1") is True
    assert _is_ip_literal("server.local") is False


def test_sync_candidates_adds_ip_fallback_only_when_useful():
    from app.services.satellite import _sync_candidates
    url = "http://server.local:9284/api/config/satellite"
    # A name host plus a cached IP yields the IP fallback as a second candidate.
    assert _sync_candidates(url, "server.local", "192.168.1.50") == [
        url, "http://192.168.1.50:9284/api/config/satellite"
    ]
    # No cached IP, or the host is already that IP, means no extra candidate.
    assert _sync_candidates(url, "server.local", "") == [url]
    ip_url = "http://192.168.1.50:9284/x"
    assert _sync_candidates(ip_url, "192.168.1.50", "192.168.1.50") == [ip_url]


def test_sync_candidates_adds_mdns_fallback_for_ip_config(monkeypatch):
    """A satellite configured with a bare IP falls back to the server's
    advertised <host>.local when the IP stops working (FoodAssistant-k9a8)."""
    from app.services.satellite import _sync_candidates
    ip_url = "http://192.168.1.50:9284/api/config/satellite"
    # Learned server hostname yields a .local candidate after the configured IP.
    assert _sync_candidates(ip_url, "192.168.1.50", "", "kitchenpi") == [
        ip_url, "http://kitchenpi.local:9284/api/config/satellite"
    ]
    # A hostname that already carries a dot is used verbatim (not double-suffixed).
    assert _sync_candidates(ip_url, "192.168.1.50", "", "kitchenpi.local") == [
        ip_url, "http://kitchenpi.local:9284/api/config/satellite"
    ]
    # No server hostname means no extra candidate.
    assert _sync_candidates(ip_url, "192.168.1.50", "") == [ip_url]
    # When already configured by that same .local name, no duplicate is added.
    name_url = "http://kitchenpi.local:9284/api/config/satellite"
    assert _sync_candidates(name_url, "kitchenpi.local", "", "kitchenpi") == [name_url]


def test_sync_falls_back_to_cached_ip_when_mdns_fails(satellite_mode, monkeypatch):
    """When the .local host does not resolve, the sync retries the cached IP."""
    from app.services import satellite as sat
    import httpx

    monkeypatch.setattr(settings, "remote_server_url", "http://server.local:9284")
    monkeypatch.setattr(settings, "remote_server_ip", "192.168.1.50")
    payload = {"ok": True, "config": {}, "expiry_defaults": [], "command": None}
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "server.local" in url:
            raise httpx.ConnectError("name resolution failed")
        return _FakeResponse(200, payload)

    with patch.object(sat.httpx, "get", side_effect=fake_get), \
            patch.object(sat, "_apply_defaults", return_value=0), \
            patch.object(sat, "_resolve_host", return_value=""), \
            patch("app.dependencies.reset_providers"):
        out = sat.sync_from_upstream()

    assert out["ok"] is True
    assert any("server.local" in u for u in calls)        # tried mDNS first
    assert any("192.168.1.50" in u for u in calls)        # then the cached IP


def test_sync_caches_resolved_server_ip(satellite_mode, monkeypatch):
    """A successful sync caches the freshly resolved server IP for next time."""
    from app.services import satellite as sat

    monkeypatch.setattr(settings, "remote_server_url", "http://server.local:9284")
    monkeypatch.setattr(settings, "remote_server_ip", "")
    payload = {"ok": True, "config": {}, "expiry_defaults": [], "command": None}

    with patch.object(sat.httpx, "get", return_value=_FakeResponse(200, payload)), \
            patch.object(sat, "_apply_defaults", return_value=0), \
            patch.object(sat, "_resolve_host", return_value="10.0.0.9"), \
            patch("app.dependencies.reset_providers"):
        out = sat.sync_from_upstream()

    assert out["ok"] is True
    assert settings.remote_server_ip == "10.0.0.9"


def test_satellite_save_drops_server_managed_fields(client, monkeypatch, tmp_path):
    # On a satellite, a user POST to /setup/save must not change a server-managed
    # field (it is pulled on each sync); the edit is dropped, not saved then
    # silently overwritten.
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "remote_server_url", "http://server:9284", raising=False)
    monkeypatch.setattr(settings, "upstream_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://server-grocy:9383", raising=False)
    assert settings.is_satellite()
    r = client.post("/setup/save", json={"grocy_base_url": "http://satellite-edit:1"})
    assert r.status_code == 200
    # The server-managed field is unchanged (the edit was dropped).
    assert settings.grocy_base_url == "http://server-grocy:9383"


def test_calibrate_touch_page_renders(client, monkeypatch):
    # Regression: the page used the deprecated TemplateResponse(name, ctx) form,
    # which crashed on the installed Starlette ('str' has no attribute headers).
    import app.routers.setup as setup_router
    monkeypatch.setattr(setup_router, "is_raspberry_pi", lambda: False, raising=False)
    r = client.get("/setup/calibrate/touch/page")
    assert r.status_code == 200
    assert "<html" in r.text.lower()
