"""A deck config the app writes survives the bridge's TOML and the deck's loader.

The dict below has the shape services/deck_config.merge_deck_config writes:
key_overrides and ha_slots whose entries carry allow_elevated true or false,
the Home Assistant policy version and the kiosk navigation socket. It goes
through the host bridge's real _streamdeck_config_toml, onto disk, and back
through the deck's config.load, so a field one side writes and the other
drops shows up here.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

from foodassistant_streamdeck import actions, config

REPO = Path(__file__).resolve().parents[1]
BRIDGE = REPO / "scripts" / "image-build" / "foodassistant-host-bridge"


def _load_bridge():
    name = "foodassistant_host_bridge_deck_roundtrip"
    spec = importlib.util.spec_from_loader(
        name, importlib.machinery.SourceFileLoader(name, str(BRIDGE)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bridge():
    if not BRIDGE.exists():
        pytest.skip("the host bridge script is not in this tree")
    return _load_bridge()


# What the app posts to the bridge after its merge (proxy form, policy on).
APP_CONFIG = {
    "base_url": "http://127.0.0.1:9284",
    "theme": "dark",
    "key_style": "rich",
    "icon_color": "full",
    "clock_format": "24",
    "idle_timeout_minutes": 0,
    "logo_when_display_off": True,
    "kiosk_cdp_url": "http://127.0.0.1:9222",
    "kiosk_nav_socket": "/run/foodassistant-kiosk/nav.sock",
    "ha_policy_version": 1,
    "ha_base_url": "http://127.0.0.1:9284/api/proxy/ha",
    "ha_token": "agent-key-for-tests",
    "keys": ["inventory", "add", "expiring", "shopping", "ha_1", "ha_2"],
    "ha_slots": [
        {"entity_id": "lock.front_door", "service": "lock.unlock", "label": "Door",
         "allow_elevated": True, "elevated_grandfathered": True},
        {"entity_id": "light.kitchen", "service": "light.toggle", "label": "Kitchen",
         "allow_elevated": False},
    ],
    "key_overrides": [
        {"slot": 0, "type": "ha_action", "entity_id": "cover.garage",
         "service": "cover.open_cover", "label": "Garage", "allow_elevated": True},
        {"slot": 1, "type": "ha_action", "entity_id": "lock.back_door",
         "service": "lock.unlock", "label": "Back", "allow_elevated": False},
        {"slot": 2, "type": "ha_action", "entity_id": "alarm_control_panel.home",
         "service": "alarm_control_panel.alarm_disarm", "label": "Alarm"},
        {"slot": 3, "type": "media", "entity_id": "media_player.kitchen",
         "action": "next", "allow_elevated": False},
        {"slot": 4, "type": "macro", "actions": ["inventory", "add"]},
    ],
    "cameras": [
        {"name": "Front Door",
         "snapshot_url": "http://127.0.0.1:9284/ui/camera/0/snapshot?name=Front%20Door"},
    ],
}


def _round_trip(bridge, tmp_path):
    text = bridge._streamdeck_config_toml(APP_CONFIG)
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return config.load(path, strict=True)


def test_policy_fields_survive(bridge, tmp_path, monkeypatch):
    monkeypatch.delenv(config.ENV_API_KEY, raising=False)
    monkeypatch.delenv(config.ENV_BASE_URL, raising=False)
    cfg = _round_trip(bridge, tmp_path)
    assert cfg.ha_policy_version == 1
    assert cfg.kiosk_nav_socket == "/run/foodassistant-kiosk/nav.sock"
    assert cfg.ha_base_url == "http://127.0.0.1:9284/api/proxy/ha"
    assert [s.get("allow_elevated") for s in cfg.ha_slots] == [True, False]
    assert cfg.ha_slots[0]["elevated_grandfathered"] is True
    assert [o.get("allow_elevated") for o in cfg.key_overrides] == \
        [True, False, None, False, None]
    assert cfg.key_overrides[4]["actions"] == ["inventory", "add"]


def test_only_the_entry_without_allow_elevated_needs_ok(bridge, tmp_path, monkeypatch):
    monkeypatch.delenv(config.ENV_API_KEY, raising=False)
    cfg = _round_trip(bridge, tmp_path)
    specs = actions.overrides_to_specs(cfg.key_overrides, 15, cfg.ha_policy_version)
    assert specs[0].ha_needs_ok is False          # allowed cover
    assert specs[1].ha_needs_ok is True           # lock, allow_elevated false
    assert specs[2].ha_needs_ok is True           # alarm, no allow_elevated
    assert specs[3].ha_needs_ok is False          # media is a standard call
    assert specs[4].kind == "macro"
    # The same entries one by one, through override_to_spec.
    marked = [actions.override_to_spec(o["slot"], o, cfg.ha_policy_version).ha_needs_ok
              for o in cfg.key_overrides[:3]]
    assert marked == [False, True, True]


def test_version_0_marks_nothing(bridge, tmp_path, monkeypatch):
    monkeypatch.delenv(config.ENV_API_KEY, raising=False)
    cfg = _round_trip(bridge, tmp_path)
    cfg.ha_policy_version = 0
    specs = actions.overrides_to_specs(cfg.key_overrides, 15, cfg.ha_policy_version)
    assert not any(s.ha_needs_ok for s in specs.values())
