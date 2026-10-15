"""The config the app merges reaches both deck versions intact.

merge_deck_config's output (proxy form, policy on, an elevated key the owner
allowed) goes through the host bridge's real _streamdeck_config_toml, onto
disk, and through two loaders: the current deck's config.load, which must keep
allow_elevated and ha_policy_version 1, and the v0.20.0 loader vendored in
tests/data/compat_v0_20_0_deck_config.json (made at build time by
scripts/compat/capture-v0-20-0-deck-config.py; the suite never runs git),
which must load it without error and ignore the field it does not know.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SERVICE = REPO / "service"
sys.path.insert(0, str(SERVICE))

from app.services import deck_config  # noqa: E402

BRIDGE = REPO / "scripts" / "image-build" / "foodassistant-host-bridge"
OLD_LOADER = REPO / "tests" / "data" / "compat_v0_20_0_deck_config.json"
AGENT_KEY = "pra1.streamdeck." + "c" * 64
ORIGIN = "http://localhost:9284"
HA_TOKEN = "ha-long-lived-token-e2e"

LOCK = {"id": "c_lock", "type": "ha_action", "entity_id": "lock.front_door",
        "service": "lock.unlock", "label": "Door", "slot": 2,
        "allow_elevated": True, "elevated_grandfathered": True}
LIGHT = {"id": "c_light", "type": "ha_action", "entity_id": "light.kitchen",
         "service": "light.turn_on", "label": "Light", "slot": 3}


def _load_bridge():
    name = "foodassistant_host_bridge_deck_e2e"
    spec = importlib.util.spec_from_loader(
        name, importlib.machinery.SourceFileLoader(name, str(BRIDGE)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bridge():
    return _load_bridge()


def _inputs(**over):
    base = {
        "weather_location": "Syracuse", "weather_units": "imperial", "theme": "dark",
        "key_style": "rich", "icon_color": "full", "ha_base_url": "http://ha.example:8123",
        "ha_token": HA_TOKEN, "ha_slots": [
            {"entity_id": "light.hall", "service": "light.toggle", "label": "Hall"}],
        "cameras": [{"name": "Porch", "ha_entity": "camera.porch"},
                    {"name": "Yard", "snapshot_url": "http://192.0.2.40/snap.jpg"}],
        "key_overrides": [LOCK, LIGHT], "idle_timeout_minutes": 10,
        "logo_when_display_off": True, "clock_format": "12", "is_satellite": False,
        "agent_key": AGENT_KEY, "proxy_origin": ORIGIN, "policy_enforced": True,
    }
    base.update(over)
    return base


def _write_toml(bridge, tmp_path, merged) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(bridge._streamdeck_config_toml(merged))
    return path


def _old_config_module():
    data = json.loads(OLD_LOADER.read_text())
    assert data["tag"] == "v0.20.0"
    pkg = types.ModuleType("olddeck")
    pkg.__path__ = []
    actions = types.ModuleType("olddeck.actions")
    actions.ACTIONS = {}
    actions.DEFAULT_ORDER = []
    saved = {k: sys.modules.get(k) for k in ("olddeck", "olddeck.actions", "olddeck.config")}
    sys.modules["olddeck"] = pkg
    sys.modules["olddeck.actions"] = actions
    try:
        mod = types.ModuleType("olddeck.config")
        mod.__package__ = "olddeck"
        sys.modules["olddeck.config"] = mod
        exec(compile(data["source"], "olddeck/config.py", "exec"), mod.__dict__)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def test_current_deck_keeps_allow_elevated_and_the_policy_version(bridge, tmp_path):
    from foodassistant_streamdeck import config as deck_cfg
    merged, form = deck_config.merge_deck_config({"rotation": 90, "brightness": 50}, _inputs())
    assert form == "proxy" and merged["ha_policy_version"] == 1
    path = _write_toml(bridge, tmp_path, merged)
    cfg = deck_cfg.load(path)
    assert cfg.ha_policy_version == 1
    lock = next(o for o in cfg.key_overrides if o.get("id") == "c_lock")
    assert lock["allow_elevated"] is True
    assert cfg.ha_token == AGENT_KEY
    assert cfg.ha_base_url == ORIGIN + "/api/proxy/ha"
    assert HA_TOKEN not in path.read_text()


def test_v0_20_0_deck_loads_the_same_file(bridge, tmp_path):
    old = _old_config_module()
    merged, _ = deck_config.merge_deck_config({"rotation": 90}, _inputs())
    path = _write_toml(bridge, tmp_path, merged)
    cfg = old.load(path, strict=True)
    assert cfg.ha_base_url == ORIGIN + "/api/proxy/ha"
    assert cfg.ha_token == AGENT_KEY
    assert [c["name"] for c in cfg.cameras] == ["Porch", "Yard"]
    assert cfg.cameras[0]["snapshot_url"] == ORIGIN + "/ui/camera/0/snapshot?name=Porch"
    assert [o.get("id") for o in cfg.key_overrides] == ["c_lock", "c_light"]
    assert not hasattr(cfg, "ha_policy_version")


def test_v0_20_0_deck_loads_the_legacy_form_too(bridge, tmp_path):
    old = _old_config_module()
    merged, form = deck_config.merge_deck_config(
        {}, _inputs(agent_key="", proxy_origin="", policy_enforced=False))
    assert form == "legacy" and merged["ha_policy_version"] == 0
    cfg = old.load(_write_toml(bridge, tmp_path, merged), strict=True)
    assert cfg.ha_token == HA_TOKEN
    assert cfg.cameras[0]["ha_entity"] == "camera.porch"
