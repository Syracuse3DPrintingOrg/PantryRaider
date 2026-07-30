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

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import satellite as sat  # noqa: E402


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
