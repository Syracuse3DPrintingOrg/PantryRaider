"""A satellite must not apply poisoned backend config over plaintext HTTP
(FoodAssistant-619i).

The server signs the config block it returns with HMAC keyed by the shared API
key, over a nonce the satellite chose. The satellite applies credential-bearing
fields ONLY when that signature verifies. An unsigned response (an old server, or
a naive LAN impostor) can still refresh harmless fields but can never overwrite
grocy_base_url, an AI key, or the HA token; a tampered signature is refused
outright.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import satellite as sat  # noqa: E402

_PULLED = "_mealie_connected_pulled"


@pytest.fixture(autouse=True)
def _restore_pulled_mealie_flag():
    """Syncs here store the pulled mealie_connected answer on the shared
    settings object, outside any field monkeypatch restores. Put it back so a
    later test file never inherits it."""
    had = _PULLED in settings.__dict__
    old = settings.__dict__.get(_PULLED)
    yield
    if had:
        settings.__dict__[_PULLED] = old
    else:
        settings.__dict__.pop(_PULLED, None)


# --- pure signing -----------------------------------------------------------

def test_signature_is_deterministic_and_key_dependent():
    cfg = {"grocy_base_url": "http://server:9383", "ui_theme": "dark"}
    a = sat.config_signature("KEY-A", "nonce1", cfg)
    assert a == sat.config_signature("KEY-A", "nonce1", cfg)  # deterministic
    assert a != sat.config_signature("KEY-B", "nonce1", cfg)  # key matters
    assert a != sat.config_signature("KEY-A", "nonce2", cfg)  # nonce matters


def test_signature_ok_checks():
    cfg = {"x": 1}
    good = sat.config_signature("K", "N", cfg)
    assert sat.signature_ok("K", "N", cfg, good) is True
    assert sat.signature_ok("K", "N", cfg, "deadbeef") is False
    assert sat.signature_ok("K", "N", cfg, "") is False
    assert sat.signature_ok("", "N", cfg, good) is False


def test_sensitive_field_classifier():
    for f in ("grocy_base_url", "grocy_api_key", "gemini_api_key",
              "streamdeck_ha_token", "ollama_base_url", "mealie_public_url",
              "beszel_url", "ai_extra_keys",
              # value-carrying / behavior-steering fields the name rule misses
              "streamdeck_cameras", "streamdeck_key_overrides",
              "fleet_label_printer_queue", "fleet_document_printer_queue",
              "vision_provider", "enrich_provider",
              "recipes_backend", "shopping_backend", "recipe_source"):
        assert sat._is_sensitive_field(f) is True
    for f in ("ui_theme", "clock_format", "streamdeck_weather_location",
              "auto_update", "timezone"):
        assert sat._is_sensitive_field(f) is False


def test_apply_config_ignores_non_dict_config():
    # A hostile/malformed response where config is a string must not raise (the
    # 'field in config' substring trap) and must apply nothing.
    assert sat._apply_config("ui_theme", allow_sensitive=False) == []
    assert sat._apply_config(["grocy_base_url"], allow_sensitive=True) == []


# --- apply gating -----------------------------------------------------------

def test_apply_config_skips_sensitive_when_not_allowed(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "grocy_base_url", "http://trusted:9383")
    monkeypatch.setattr(settings, "ui_theme", "light")
    applied = sat._apply_config(
        {"grocy_base_url": "http://attacker:9383", "ui_theme": "dark"},
        allow_sensitive=False)
    # The backend URL was NOT overwritten; the harmless theme was.
    assert "grocy_base_url" not in applied
    assert settings.grocy_base_url == "http://trusted:9383"
    assert "ui_theme" in applied
    assert settings.ui_theme == "dark"


# --- full sync paths --------------------------------------------------------

@pytest.fixture
def satellite_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(settings, "remote_server_url", "http://server:9284")
    monkeypatch.setattr(settings, "upstream_api_key", "shared-key")
    monkeypatch.setattr(settings, "device_id", "dev-1")
    monkeypatch.setattr(settings, "grocy_base_url", "http://trusted:9383")
    monkeypatch.setattr(settings, "ui_theme", "light")
    object.__setattr__(settings, "server_sourced_fields", set())
    yield


class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


def _run_sync(payload_builder):
    """Run a sync where httpx.get returns whatever payload_builder(nonce) gives."""
    def _get(url, headers=None, timeout=None, **kw):
        return _Resp(payload_builder(headers.get("X-Config-Nonce", "")))
    with patch.object(sat.httpx, "get", side_effect=_get), \
            patch.object(sat, "_apply_defaults", return_value=0), \
            patch.object(sat, "_resolve_host", return_value=""), \
            patch("app.dependencies.reset_providers"):
        return sat.sync_from_upstream()


def test_signed_response_applies_backend_config(satellite_mode):
    cfg = {"grocy_base_url": "http://server:9383", "ui_theme": "dark"}

    def _build(nonce):
        return {"ok": True, "config": cfg,
                "config_nonce": nonce,
                "config_signature": sat.config_signature("shared-key", nonce, cfg)}

    out = _run_sync(_build)
    assert out["ok"] is True
    assert "grocy_base_url" in out["applied"]
    assert settings.grocy_base_url == "http://server:9383"


def test_unsigned_response_keeps_backend_config(satellite_mode):
    # A naive impostor (or an old server) sends no signature: the harmless theme
    # applies, but the backend URL is NOT overwritten.
    def _build(nonce):
        return {"ok": True,
                "config": {"grocy_base_url": "http://attacker:9383",
                           "ui_theme": "dark"}}

    out = _run_sync(_build)
    assert out["ok"] is True
    assert "grocy_base_url" not in out["applied"]
    assert settings.grocy_base_url == "http://trusted:9383"  # unchanged
    assert settings.ui_theme == "dark"  # harmless field still refreshed


def test_tampered_signature_is_refused_entirely(satellite_mode):
    def _build(nonce):
        return {"ok": True,
                "config": {"grocy_base_url": "http://attacker:9383",
                           "ui_theme": "dark"},
                "config_nonce": nonce,
                "config_signature": "0" * 64}  # present but wrong

    out = _run_sync(_build)
    assert out["ok"] is False
    assert "signature" in (out["error"] or "")
    # Nothing applied, backend config and theme both untouched.
    assert settings.grocy_base_url == "http://trusted:9383"
    assert settings.ui_theme == "light"


def test_unsigned_response_cannot_inject_a_camera_url(satellite_mode):
    # streamdeck_cameras carries a snapshot_url the app fetches server-side, so
    # an unsigned pull must not be able to add one (FoodAssistant-619i).
    from app.config import settings as _s
    object.__setattr__(_s, "streamdeck_cameras", [])

    def _build(nonce):
        return {"ok": True,
                "config": {"streamdeck_cameras": [
                    {"name": "evil", "snapshot_url": "http://127.0.0.1:9299/reboot"}],
                    "ui_theme": "dark"}}

    out = _run_sync(_build)
    assert out["ok"] is True
    assert "streamdeck_cameras" not in out["applied"]
    assert settings.streamdeck_cameras == []  # not injected
    assert settings.ui_theme == "dark"  # harmless field still refreshed


def test_non_object_body_does_not_crash_sync(satellite_mode):
    def _get(url, headers=None, timeout=None, **kw):
        class _R:
            status_code = 200
            text = "not json"
            def json(self_inner):
                return "just a string"  # a non-object JSON body
        return _R()
    with patch.object(sat.httpx, "get", side_effect=_get), \
            patch.object(sat, "_resolve_host", return_value=""):
        out = sat.sync_from_upstream()
    assert out["ok"] is False  # refused, but no exception escaped
    assert settings.grocy_base_url == "http://trusted:9383"


def test_wrong_key_signature_is_refused(satellite_mode):
    cfg = {"grocy_base_url": "http://attacker:9383"}

    def _build(nonce):
        # Signed, but with a key the satellite does not hold.
        return {"ok": True, "config": cfg, "config_nonce": nonce,
                "config_signature": sat.config_signature("other-key", nonce, cfg)}

    out = _run_sync(_build)
    assert out["ok"] is False
    assert settings.grocy_base_url == "http://trusted:9383"


# --- the pull no longer carries the Grocy and Mealie keys (R1) --------------
#
# A satellite reaches Grocy and Mealie only through the server's /api/proxy
# hop, which adds the server's own credentials, so the admin keys stay on the
# server. These pin that the pull leaves them out, and that a satellite on
# either side of the change keeps working with that payload.

_OLD_FIELDS = Path(__file__).parent / "data" / "r1_fleet_pull_fields_v0_20_0.json"


def test_pull_fields_leave_out_backend_keys():
    from app.config import SATELLITE_PULL_FIELDS
    assert "grocy_api_key" not in SATELLITE_PULL_FIELDS
    assert "mealie_api_key" not in SATELLITE_PULL_FIELDS
    # The addresses still ride along: MealieClient.configured reads
    # mealie_base_url on a satellite to learn whether the server has a Mealie.
    for f in ("grocy_base_url", "grocy_public_url", "mealie_base_url", "mealie_public_url"):
        assert f in SATELLITE_PULL_FIELDS


def test_server_pull_payload_has_no_backend_keys(monkeypatch, tmp_path):
    import os
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "api_key", "shared-key")
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(settings, "grocy_api_key", "grocy-admin-key")
    monkeypatch.setattr(settings, "mealie_api_key", "mealie-admin-key")
    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie:9000")
    cwd = os.getcwd()
    os.chdir(SERVICE)
    try:
        r = TestClient(app, client=("192.168.1.20", 50000)).get(
            "/api/config/satellite",
            headers={"X-API-Key": "shared-key", "X-Config-Nonce": "n1"})
    finally:
        os.chdir(cwd)
    assert r.status_code == 200
    body = r.json()
    assert "grocy_api_key" not in body["config"]
    assert "mealie_api_key" not in body["config"]
    assert "grocy-admin-key" not in r.text and "mealie-admin-key" not in r.text
    assert body["config"]["mealie_base_url"] == "http://mealie:9000"
    # Still signed, so a satellite applies the backend addresses.
    assert sat.signature_ok("shared-key", "n1", body["config"], body["config_signature"])


def _new_server_payload(nonce):
    """What an R1 server sends: its pull fields, signed."""
    from app.config import SATELLITE_PULL_FIELDS
    cfg = {"grocy_base_url": "http://grocy:80", "mealie_base_url": "http://mealie:9000",
           "ui_theme": "dark"}
    assert set(cfg) <= set(SATELLITE_PULL_FIELDS)
    return {"ok": True, "config": cfg, "config_nonce": nonce,
            "config_signature": sat.config_signature("shared-key", nonce, cfg)}


def test_satellite_keeps_working_without_pulled_keys(satellite_mode, monkeypatch):
    # A satellite that pulled the keys from an older server keeps them (the
    # sync never blanks a field the payload leaves out), and its clients go
    # through the proxy either way, so neither key is needed.
    monkeypatch.setattr(settings, "grocy_api_key", "pulled-earlier")
    monkeypatch.setattr(settings, "mealie_api_key", "")
    out = _run_sync(_new_server_payload)
    assert out["ok"] is True
    assert "grocy_base_url" in out["applied"]
    assert "grocy_api_key" not in out["applied"]
    assert settings.grocy_api_key == "pulled-earlier"
    from app.services.grocy import GrocyClient
    from app.services.mealie import MealieClient
    g = GrocyClient()
    assert g.base == "http://server:9284/api/proxy/grocy/api"
    assert g.headers["X-API-Key"] == "shared-key"
    m = MealieClient()
    assert m.base == "http://server:9284/api/proxy/mealie"
    assert m.configured is True  # no Mealie key on this device, still enabled


def test_older_satellite_keeps_its_key_with_new_payload(satellite_mode, monkeypatch):
    # A v0.20.0 satellite still lists the keys among its pull fields. Its sync
    # loop (the same "skip a field the payload does not carry" loop, checked
    # against v0.6.0 through v0.20.0) must leave its stored key alone.
    old_fields = json.loads(_OLD_FIELDS.read_text())["fields"]
    assert "grocy_api_key" in old_fields and "mealie_api_key" in old_fields
    monkeypatch.setattr(sat, "SATELLITE_PULL_FIELDS", old_fields)
    monkeypatch.setattr(settings, "grocy_api_key", "pulled-earlier")
    monkeypatch.setattr(settings, "mealie_api_key", "mealie-earlier")
    out = _run_sync(_new_server_payload)
    assert out["ok"] is True
    assert settings.grocy_api_key == "pulled-earlier"
    assert settings.mealie_api_key == "mealie-earlier"
    assert "grocy_api_key" not in out["applied"]
    assert settings.grocy_base_url == "http://grocy:80"


def test_pull_names_the_satellite_release(satellite_mode):
    from app.config import APP_VERSION
    seen = {}

    def _build(nonce):
        return _new_server_payload(nonce)

    def _get(url, headers=None, timeout=None, **kw):
        seen.update(headers)
        return _Resp(_build(headers.get("X-Config-Nonce", "")))
    with patch.object(sat.httpx, "get", side_effect=_get), \
            patch.object(sat, "_apply_defaults", return_value=0), \
            patch.object(sat, "_resolve_host", return_value=""), \
            patch("app.dependencies.reset_providers"):
        assert sat.sync_from_upstream()["ok"] is True
    assert seen["X-PR-Client"] == f"satellite/{APP_VERSION}"


# --- Mealie on a satellite without the Mealie key -----------------------------
#
# Mealie features on a satellite key off settings.mealie_configured(), which
# used to read the pulled Mealie key. The server now sends a yes/no
# (mealie_connected) instead, and an older server's key is turned into that
# yes/no without being stored.

def test_server_computes_mealie_connected_live(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "server")
    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie:9000")
    monkeypatch.setattr(settings, "mealie_api_key", "")
    # A stale pulled value never affects a server.
    settings.mealie_connected = True
    assert settings.mealie_connected is False
    # An address alone (a fresh appliance seeds one) is not a connection.
    assert settings.mealie_configured() is False
    monkeypatch.setattr(settings, "mealie_api_key", "tok")
    assert settings.mealie_connected is True and settings.mealie_configured() is True


def test_new_satellite_uses_pulled_mealie_connected(satellite_mode, monkeypatch):
    monkeypatch.setattr(settings, "mealie_base_url", "")
    monkeypatch.setattr(settings, "mealie_api_key", "")

    def _build(nonce):
        cfg = {"mealie_base_url": "http://mealie:9000", "mealie_connected": True}
        return {"ok": True, "config": cfg, "config_nonce": nonce,
                "config_signature": sat.config_signature("shared-key", nonce, cfg)}

    out = _run_sync(_build)
    assert "mealie_connected" in out["applied"]
    assert settings.mealie_api_key == ""
    assert settings.mealie_configured() is True
    # Persisted, so another worker or a restart sees it before the next pull.
    saved = json.loads((Path(settings.data_dir) / "settings.json").read_text())
    assert saved["mealie_connected"] is True


def test_server_without_mealie_connection_keeps_it_off(satellite_mode, monkeypatch):
    # The server has a seeded Mealie address but no connection: a satellite
    # must not switch Mealie on, even holding a stale key from an old pull.
    monkeypatch.setattr(settings, "mealie_api_key", "stale")

    def _build(nonce):
        cfg = {"mealie_base_url": "http://localhost:9285", "mealie_connected": False}
        return {"ok": True, "config": cfg, "config_nonce": nonce,
                "config_signature": sat.config_signature("shared-key", nonce, cfg)}

    _run_sync(_build)
    assert settings.mealie_configured() is False


def test_upgraded_satellite_keeps_mealie_before_first_new_pull(monkeypatch):
    # Nothing pulled yet on this release: the key an older server handed out
    # still counts, so an upgraded satellite does not lose Mealie at boot.
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie:9000")
    monkeypatch.setattr(settings, "mealie_api_key", "pulled-earlier")
    settings.__dict__.pop("_mealie_connected_pulled", None)
    assert settings.mealie_configured() is True


def test_older_server_key_becomes_the_flag_and_is_not_stored(satellite_mode, monkeypatch):
    # A satellite newer than its server: the server still sends the key.
    monkeypatch.setattr(settings, "mealie_api_key", "")

    def _build(nonce):
        cfg = {"mealie_base_url": "http://mealie:9000", "mealie_api_key": "srv-key",
               "grocy_api_key": "grocy-key"}
        return {"ok": True, "config": cfg, "config_nonce": nonce,
                "config_signature": sat.config_signature("shared-key", nonce, cfg)}

    out = _run_sync(_build)
    assert out["ok"] is True
    assert "mealie_connected" in out["applied"]
    assert "mealie_api_key" not in out["applied"]
    assert settings.mealie_api_key == ""
    assert settings.mealie_configured() is True
    saved = (Path(settings.data_dir) / "settings.json").read_text()
    assert "srv-key" not in saved and "grocy-key" not in saved


def test_unsigned_payload_cannot_turn_mealie_on(satellite_mode, monkeypatch):
    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie:9000")
    monkeypatch.setattr(settings, "mealie_api_key", "")
    settings.mealie_connected = False
    out = _run_sync(lambda nonce: {"ok": True, "config": {"mealie_connected": True}})
    assert "mealie_connected" not in out["applied"]
    assert settings.mealie_configured() is False


def test_readonly_fields_cover_the_unpulled_keys():
    from app.config import SATELLITE_PULL_FIELDS, SATELLITE_READONLY_FIELDS
    assert set(SATELLITE_PULL_FIELDS) <= set(SATELLITE_READONLY_FIELDS)
    assert {"grocy_api_key", "mealie_api_key"} <= set(SATELLITE_READONLY_FIELDS)


# --- 0.21: the pull caps -------------------------------------------------------

def _caps():
    return sat._pull_caps().split(",")


def test_pull_caps_without_a_deck_claims_the_proxy(monkeypatch):
    monkeypatch.setattr(settings, "has_streamdeck", False)
    monkeypatch.setattr(settings, "gadget_ha_enabled", False)
    monkeypatch.setattr(settings, "gadgets_enabled", False)
    assert _caps() == ["relay1", "deckproxy1"]


def test_pull_caps_keep_the_token_for_probe_target_alerts(monkeypatch):
    """Probe target alerts go to Home Assistant with the local token, so a
    satellite with a Bluetooth thermometer target keeps receiving it."""
    monkeypatch.setattr(settings, "has_streamdeck", False)
    monkeypatch.setattr(settings, "gadget_ha_enabled", False)
    monkeypatch.setattr(settings, "gadgets_enabled", True)
    monkeypatch.setattr(settings, "gadget_devices", [
        {"id": "AA:BB:CC:DD:EE:FF", "name": "Roast", "protocol": "inkbird",
         "targets": {"1": {"temp_c": 57.0, "direction": "above"}}}])
    assert _caps() == ["relay1"]
    # A thermometer with no targets sends no alerts.
    monkeypatch.setattr(settings, "gadget_devices", [
        {"id": "AA:BB:CC:DD:EE:FF", "name": "Roast", "protocol": "inkbird",
         "targets": {}}])
    assert _caps() == ["relay1", "deckproxy1"]
    # Thermometers switched off send nothing either.
    monkeypatch.setattr(settings, "gadgets_enabled", False)
    monkeypatch.setattr(settings, "gadget_devices", [
        {"id": "AA:BB:CC:DD:EE:FF", "targets": {"1": {"temp_c": 57.0}}}])
    assert _caps() == ["relay1", "deckproxy1"]


def test_pull_caps_follow_the_deck_push_form(monkeypatch):
    from app.services import deck_config
    monkeypatch.setattr(settings, "has_streamdeck", True)
    monkeypatch.setattr(settings, "gadget_ha_enabled", False)
    monkeypatch.setattr(settings, "gadgets_enabled", False)
    monkeypatch.setattr(deck_config, "last_push_form", lambda: "proxy")
    assert _caps() == ["relay1", "deckproxy1"]
    monkeypatch.setattr(deck_config, "last_push_form", lambda: "legacy")
    assert _caps() == ["relay1"]
    monkeypatch.setattr(deck_config, "last_push_form", lambda: None)
    assert _caps() == ["relay1"]


def test_pull_caps_keep_the_token_for_local_thermometers(monkeypatch):
    monkeypatch.setattr(settings, "has_streamdeck", False)
    monkeypatch.setattr(settings, "gadget_ha_enabled", True)
    monkeypatch.setattr(settings, "gadgets_enabled", False)
    monkeypatch.setattr(settings, "gadget_ha_entities", ["sensor.fridge_temp"])
    monkeypatch.setattr(settings, "gadget_ha_hygrometers", [])
    assert _caps() == ["relay1"]
    # Switched on but nothing configured: nothing reads Home Assistant here.
    monkeypatch.setattr(settings, "gadget_ha_entities", [])
    assert _caps() == ["relay1", "deckproxy1"]
    monkeypatch.setattr(settings, "gadget_ha_hygrometers",
                        [{"temperature": "sensor.cellar_temp"}])
    assert _caps() == ["relay1"]


def test_pull_caps_fall_back_to_relay_only_on_any_error(monkeypatch):
    from app.services import deck_config

    def _boom():
        raise RuntimeError("unreadable")
    monkeypatch.setattr(settings, "has_streamdeck", True)
    monkeypatch.setattr(settings, "gadget_ha_enabled", False)
    monkeypatch.setattr(deck_config, "last_push_form", _boom)
    assert _caps() == ["relay1"]


def test_pull_caps_fall_back_when_a_module_cannot_load(monkeypatch):
    import app.services as services_pkg
    monkeypatch.setattr(settings, "has_streamdeck", True)
    monkeypatch.setattr(settings, "gadget_ha_enabled", False)
    monkeypatch.delattr(services_pkg, "deck_config")
    monkeypatch.setitem(sys.modules, "app.services.deck_config", None)
    assert _caps() == ["relay1"]


def test_pull_sends_the_caps_and_trust(satellite_mode, monkeypatch):
    seen = {}
    monkeypatch.setattr(sat, "_pull_caps", lambda: "relay1,deckproxy1")
    monkeypatch.setattr(sat, "_device_trust", lambda: "SHA256:abc")

    def _get(url, headers=None, timeout=None, **kw):
        seen.update(headers)
        return _Resp(_new_server_payload(headers.get("X-Config-Nonce", "")))
    with patch.object(sat.httpx, "get", side_effect=_get), \
            patch.object(sat, "_apply_defaults", return_value=0), \
            patch.object(sat, "_resolve_host", return_value=""), \
            patch("app.dependencies.reset_providers"):
        assert sat.sync_from_upstream()["ok"] is True
    assert seen["X-PR-Caps"] == "relay1,deckproxy1"
    assert seen["X-Device-Trust"] == "SHA256:abc"
    assert seen["X-API-Key"] == "shared-key"
    # Exactly one spelling of each header.
    assert len([k for k in seen if k.lower() == "x-pr-caps"]) == 1


def test_device_trust_is_none_off_a_pi(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "server")
    assert sat._device_trust() == "none"


def test_device_trust_reads_the_bridge_once(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(sat, "bridge_headers", lambda: {})
    calls = []

    class _R:
        status_code = 200

        def json(self):
            return {"ok": True, "trust_fp": "SHA256:one,SHA256:two"}

    def _get(url, **kw):
        calls.append(url)
        return _R()
    monkeypatch.setattr(sat.httpx, "get", _get)
    assert sat._device_trust() == "SHA256:one,SHA256:two"
    assert sat._device_trust() == "SHA256:one,SHA256:two"
    assert calls == ["http://127.0.0.1:9299/update/state"]


def test_device_trust_404_reads_as_none(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(sat, "bridge_headers", lambda: {})

    class _R:
        status_code = 404

    monkeypatch.setattr(sat.httpx, "get", lambda url, **kw: _R())
    assert sat._device_trust() == "none"


# --- 0.21: routing --------------------------------------------------------------

def _app_client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "api_key", "shared-key")
    monkeypatch.setattr(settings, "auth_password", "")
    return TestClient(app, client=("192.168.1.20", 50000))


def test_fleet_relay_route_is_served_and_needs_a_key(monkeypatch, tmp_path):
    c = _app_client(monkeypatch, tmp_path)
    r = c.post("/api/fleet/ai", json={"role": "vision", "op": "health_check", "args": {}})
    assert r.status_code == 401
    assert r.json() == {"detail": "Unauthorized"}
    r = c.get("/api/config/satellite", headers={"X-API-Key": "shared-key"})
    assert r.status_code == 200 and r.json()["ok"] is True


# --- 0.21: what an unsigned reply may change ---------------------------------------

def test_unsigned_reply_applies_only_cosmetic_fields(satellite_mode, monkeypatch):
    from app.config import UNSIGNED_COSMETIC_FIELDS
    monkeypatch.setattr(settings, "timezone", "UTC")
    monkeypatch.setattr(settings, "auto_update", False)
    monkeypatch.setattr(settings, "ha_events_enabled", False)
    monkeypatch.setattr(settings, "remote_server_host", "")
    pushes = []
    monkeypatch.setattr(sat, "_push_streamdeck_settings", lambda *a, **k: pushes.append(1))
    monkeypatch.setattr(sat, "_push_timezone", lambda *a, **k: pushes.append("tz"))
    mirrored = []

    def _build(nonce):
        return {"ok": True,
                "config": {"ui_theme": "dark", "timezone": "America/Chicago",
                           "auto_update": True, "ha_events_enabled": True},
                "server_hostname": "impostor", "server_version": "9.9.9",
                "streamdeck_profiles": [{"name": "evil"}],
                "gadget_alarms": [{"key": "k", "started_epoch": 1}],
                "command": "resync"}

    with patch.object(sat, "_apply_profiles") as ap, \
            patch.object(sat, "mirror_alarms", side_effect=mirrored.append):
        out = _run_sync(_build)
    assert out["ok"] is True
    assert out["applied"] == ["ui_theme"]
    assert set(out["applied"]) <= set(UNSIGNED_COSMETIC_FIELDS)
    assert settings.ui_theme == "dark"
    assert settings.timezone == "UTC"
    assert settings.auto_update is False
    assert settings.ha_events_enabled is False
    assert out["command"] is None
    assert settings.remote_server_host == ""
    assert sat.last_server_version() != "9.9.9"
    ap.assert_not_called()
    assert mirrored == []
    assert "tz" not in pushes


def _signed(cfg, nonce, extras=None, key="shared-key"):
    body = {"ok": True, "config": cfg, "config_nonce": nonce,
            "config_signature": sat.config_signature(key, nonce, cfg)}
    body.update(extras or {})
    return body


_EXTRAS = {"server_hostname": "kitchen-server", "server_version": "0.21.0",
           "expiry_defaults": [], "streamdeck_profiles": [{"name": "p"}],
           "gadget_config": {"gadgets_enabled": False}, "gadget_alarms": [],
           "command": None}


def test_verified_reply_with_a_wrong_digest_applies_config_but_no_extras(
        satellite_mode, monkeypatch):
    monkeypatch.setattr(settings, "remote_server_host", "")

    def _build(nonce):
        cfg = {"grocy_base_url": "http://server:9383", "ha_policy_version": 0,
               "extras_sha256": "0" * 64}
        return _signed(cfg, nonce, _EXTRAS)

    with patch.object(sat, "_apply_profiles") as ap:
        out = _run_sync(_build)
    assert out["ok"] is True
    assert settings.grocy_base_url == "http://server:9383"
    assert settings.remote_server_host == ""
    ap.assert_not_called()


def test_verified_reply_with_the_right_digest_applies_extras(satellite_mode, monkeypatch):
    monkeypatch.setattr(settings, "remote_server_host", "")

    def _build(nonce):
        cfg = {"grocy_base_url": "http://server:9383", "ha_policy_version": 0,
               "extras_sha256": sat.extras_digest(_EXTRAS)}
        return _signed(cfg, nonce, _EXTRAS)

    with patch.object(sat, "_apply_profiles") as ap:
        out = _run_sync(_build)
    assert out["ok"] is True
    assert settings.remote_server_host == "kitchen-server"
    ap.assert_called_once_with([{"name": "p"}])
    assert sat.last_server_version() == "0.21.0"


# --- 0.21: the relay block and the scrub ----------------------------------------

_BLOCK = {"v": 1, "caps": ["ai", "recipes_external", "ha", "camera"],
          "ai_available": True, "enrich_available": True, "ha_available": True,
          "legacy_credentials": "auto", "server_has_password": False}


def _seed_copies(tmp_path, values):
    data = dict(values)
    (tmp_path / "settings.json").write_text(json.dumps(data))
    snap = tmp_path / ".pre-restore" / "20260901-000000"
    snap.mkdir(parents=True)
    (snap / "settings.json").write_text(json.dumps(data))
    return snap / "settings.json"


def test_relay_block_persists_and_clears_old_key_copies(satellite_mode, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "gemini_api_key", "old-gemini")
    monkeypatch.setattr(settings, "spoonacular_api_key", "old-spoon")
    monkeypatch.setattr(settings, "streamdeck_ha_token", "ha-token")
    monkeypatch.setattr(settings, "fleet_relay", {})
    snapshot = {"gemini_api_key": "own-gemini-from-before"}
    monkeypatch.setattr(settings, "hosted_config_snapshot", snapshot)
    pre = _seed_copies(tmp_path, {"gemini_api_key": "old-gemini",
                                  "spoonacular_api_key": "old-spoon",
                                  "streamdeck_ha_token": "ha-token",
                                  "hosted_config_snapshot": snapshot})

    def _build(nonce):
        cfg = {"grocy_base_url": "http://server:9383", "ha_policy_version": 1,
               "streamdeck_ha_token": "ha-token", "fleet_relay": _BLOCK,
               "extras_sha256": sat.extras_digest(_EXTRAS)}
        return _signed(cfg, nonce, _EXTRAS)

    out = _run_sync(_build)
    assert out["ok"] is True
    assert settings.fleet_relay == _BLOCK
    assert settings.upstream_ha_policy_version == 1
    assert settings.gemini_api_key == "" and settings.spoonacular_api_key == ""
    # The server sent the token, so it stays.
    assert settings.streamdeck_ha_token == "ha-token"
    live = json.loads((tmp_path / "settings.json").read_text())
    bak = json.loads((tmp_path / "settings.json.bak").read_text())
    old = json.loads(pre.read_text())
    for doc in (live, bak, old):
        assert doc.get("gemini_api_key", "") == ""
        assert doc.get("spoonacular_api_key", "") == ""
    assert live["streamdeck_ha_token"] == "ha-token"
    assert old["streamdeck_ha_token"] == "ha-token"
    # This device's own settings from before it became a satellite stay.
    assert live["hosted_config_snapshot"] == snapshot
    assert old["hosted_config_snapshot"] == snapshot
    assert settings.hosted_config_snapshot == snapshot
    assert "old-gemini" not in (tmp_path / "settings.json.bak").read_text()


def test_withheld_token_is_cleared(satellite_mode, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "streamdeck_ha_token", "ha-token")
    pre = _seed_copies(tmp_path, {"streamdeck_ha_token": "ha-token"})

    def _build(nonce):
        cfg = {"ha_policy_version": 1, "fleet_relay": _BLOCK,
               "streamdeck_cameras": [{"name": "Door", "relay": True, "has_stream": True}]}
        return _signed(cfg, nonce)

    pushes = []
    monkeypatch.setattr(sat, "_push_streamdeck_settings", lambda *a, **k: pushes.append(1))
    _run_sync(_build)
    assert settings.streamdeck_ha_token == ""
    assert json.loads(pre.read_text())["streamdeck_ha_token"] == ""
    assert "ha-token" not in (tmp_path / "settings.json.bak").read_text()
    assert pushes  # the deck follows


def test_reply_without_a_block_goes_back_to_local_keys(satellite_mode, monkeypatch):
    monkeypatch.setattr(settings, "fleet_relay", dict(_BLOCK))
    monkeypatch.setattr(settings, "upstream_ha_policy_version", 1)
    monkeypatch.setattr(settings, "gemini_api_key", "")

    def _build(nonce):
        # A server rolled back to 0.20: no block, the keys again.
        return _signed({"gemini_api_key": "server-gemini"}, nonce)

    out = _run_sync(_build)
    assert out["ok"] is True
    assert settings.fleet_relay == {}
    assert settings.upstream_ha_policy_version == 0
    assert settings.gemini_api_key == "server-gemini"


def test_relay_reply_never_takes_service_keys(satellite_mode, monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "")

    def _build(nonce):
        return _signed({"gemini_api_key": "sent-anyway", "fleet_relay": _BLOCK}, nonce)

    out = _run_sync(_build)
    assert "gemini_api_key" not in out["applied"]
    assert settings.gemini_api_key == ""


def test_unsigned_block_is_ignored(satellite_mode, monkeypatch):
    monkeypatch.setattr(settings, "fleet_relay", {})
    monkeypatch.setattr(settings, "gemini_api_key", "keep-me")
    out = _run_sync(lambda nonce: {"ok": True,
                                   "config": {"fleet_relay": _BLOCK, "ha_policy_version": 1}})
    assert out["ok"] is True
    assert settings.fleet_relay == {}
    assert settings.upstream_ha_policy_version == 0
    assert settings.gemini_api_key == "keep-me"


@pytest.mark.parametrize("sent, stored", [(1, 1), (0, 0), (2, 2), (-1, 0), ("1", 0),
                                           (True, 0), (None, 0)])
def test_upstream_policy_version_follows_the_reply(satellite_mode, monkeypatch, sent, stored):
    monkeypatch.setattr(settings, "upstream_ha_policy_version", 5)

    def _build(nonce):
        cfg = {"ui_theme": "dark"}
        if sent is not None:
            cfg["ha_policy_version"] = sent
        return _signed(cfg, nonce)

    _run_sync(_build)
    assert settings.upstream_ha_policy_version == stored


def test_parked_stack_keeps_its_own_grocy_key(satellite_mode, monkeypatch):
    # A device switched from hosting its own stack keeps its local Grocy key
    # (Switch back needs it); only keys a server handed out are cleared.
    monkeypatch.setattr(settings, "hosted_stack_parked", True)
    monkeypatch.setattr(settings, "hosted_config_snapshot", {"grocy_base_url": "x"})
    monkeypatch.setattr(settings, "grocy_api_key", "local-stack-key")
    monkeypatch.setattr(settings, "gemini_api_key", "pulled")
    _run_sync(lambda nonce: _signed({"fleet_relay": _BLOCK}, nonce))
    assert settings.grocy_api_key == "local-stack-key"
    assert settings.gemini_api_key == ""
