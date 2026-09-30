"""Prebuilt hardware presets (FoodAssistant-kl5n).

A preset is a plain-data bundle of hardware settings applied in one click from
the setup wizard or the Screen & Sleep pane. These tests pin the registry to
real settings, exercise the apply route, and cover the streamdeck_rotation
plumbing the preset relies on.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings, HARDWARE_PRESETS, DISPLAY_ROTATIONS, UI_SCALES, DISPLAY_TYPES  # noqa: E402


def _setting_keys() -> set[str]:
    return set(type(settings).model_fields.keys())


def test_registry_shape_and_keys_are_real_settings():
    assert HARDWARE_PRESETS, "at least one preset must ship"
    real = _setting_keys()
    for pid, preset in HARDWARE_PRESETS.items():
        assert isinstance(pid, str) and pid
        assert preset.get("label"), f"{pid} needs a label"
        assert preset.get("description"), f"{pid} needs a description"
        bundle = preset.get("settings")
        assert isinstance(bundle, dict) and bundle, f"{pid} needs a settings bundle"
        for key in bundle:
            assert key in real, f"{pid} sets unknown setting {key!r}"


def test_registry_values_are_valid():
    for pid, preset in HARDWARE_PRESETS.items():
        b = preset["settings"]
        if "display_rotation" in b:
            assert b["display_rotation"] in DISPLAY_ROTATIONS
        if "streamdeck_rotation" in b:
            assert b["streamdeck_rotation"] in DISPLAY_ROTATIONS
        if "ui_scale" in b:
            assert b["ui_scale"] in UI_SCALES
        if "display_type" in b:
            assert b["display_type"] in DISPLAY_TYPES
        if "streamdeck_key_count" in b:
            assert b["streamdeck_key_count"] in (6, 15, 32)


def test_bandit_v1_bundle():
    b = HARDWARE_PRESETS["bandit-v1"]["settings"]
    assert b["display_rotation"] == 270
    assert b["ui_scale"] == "small"
    assert b["display_type"] == "generic"
    assert b["display_touch"] is True
    assert b["has_streamdeck"] is True
    assert b["streamdeck_key_count"] == 15
    assert b["streamdeck_rotation"] == 90


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_password", "")
    # Seed a settings.json holding an unrelated value the apply must preserve.
    (tmp_path / "settings.json").write_text(json.dumps({"ui_theme": "forest"}))
    try:
        # Off a Pi the deck push is a no-op, so no host bridge is contacted.
        with patch("app.hardware.is_raspberry_pi", return_value=False):
            yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_apply_valid_preset_saves_only_the_bundle(client, tmp_path):
    r = client.post("/setup/preset/apply", json={"preset": "bandit-v1"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["preset"] == "bandit-v1"
    assert body["applied"] == HARDWARE_PRESETS["bandit-v1"]["settings"]

    on_disk = json.loads((tmp_path / "settings.json").read_text())
    # Every preset field is persisted.
    for key, val in HARDWARE_PRESETS["bandit-v1"]["settings"].items():
        assert on_disk[key] == val
    # An unrelated setting is untouched (only the bundle changed).
    assert on_disk["ui_theme"] == "forest"
    # A setting the preset does not name is never written.
    assert "grocy_base_url" not in on_disk


def test_apply_unknown_preset_is_400(client):
    r = client.post("/setup/preset/apply", json={"preset": "does-not-exist"})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_apply_missing_preset_is_400(client):
    r = client.post("/setup/preset/apply", json={})
    assert r.status_code == 400


# -- streamdeck_rotation plumbing -------------------------------------------

def test_streamdeck_rotation_is_saveable():
    from app.config import _SAVEABLE
    assert "streamdeck_rotation" in _SAVEABLE


def test_merge_stamps_rotation_when_given():
    from app.services.satellite import _merge_streamdeck_settings
    merged = _merge_streamdeck_settings({"rotation": 0}, "", "f", "dark", rotation=90)
    assert merged["rotation"] == 90


def test_merge_keeps_existing_rotation_when_none():
    from app.services.satellite import _merge_streamdeck_settings
    # A default (None) must not clobber a rotation already in config.toml.
    merged = _merge_streamdeck_settings({"rotation": 270}, "", "f", "dark", rotation=None)
    assert merged["rotation"] == 270


def test_merge_ignores_invalid_rotation():
    from app.services.satellite import _merge_streamdeck_settings
    merged = _merge_streamdeck_settings({"rotation": 90}, "", "f", "dark", rotation=45)
    assert merged["rotation"] == 90
