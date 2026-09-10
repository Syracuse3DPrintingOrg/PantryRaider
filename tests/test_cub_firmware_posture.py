"""The Bandit Cub firmware's security posture and licensing, pinned.

Two launch blockers live here (FoodAssistant-ympla, FoodAssistant-yhids) and
both are the kind that come back quietly:

* A shared factory image cannot hold a per-device secret, so the published
  firmware closes the push-OTA path entirely rather than shipping a password
  that is printed in a public repository. Someone adding ``platform: esphome``
  back to the base package for convenience would reopen arbitrary code
  execution on every Cub on the network.
* The firmware is compiled against ESPHome's GPL-3.0 C++ runtime, so it cannot
  travel under the repository's PolyForm Noncommercial license. GPL-3.0
  forbids adding restrictions like that, and the source has to be reachable.

None of this needs a device or a build; it is all readable from the tree.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_ESPHOME = _ROOT / "esphome"
_BASE = _ESPHOME / "packages" / "cub-base.yaml"
_COMPONENT = _ESPHOME / "components" / "pantry_raider"


def _base_config() -> dict:
    """cub-base.yaml as data. ESPHome tags (!secret, !lambda) are not YAML, so
    they are ignored rather than resolved: this reads structure, not values."""
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor(
        "!", lambda loader, suffix, node: None)
    return yaml.load(_BASE.read_text(), Loader=_Loader)


def test_published_firmware_refuses_pushed_updates():
    """Only the pull path may ship. platform: esphome accepts a firmware image
    from anyone on the network, and a shared image cannot password protect it."""
    platforms = [entry.get("platform") for entry in _base_config()["ota"]]
    assert "http_request" in platforms, "the Cub must still pull updates from its server"
    assert "esphome" not in platforms, (
        "cub-base.yaml must not offer push OTA: a shared factory image cannot "
        "carry a password, so this would let anyone on the network run code on "
        "every Cub. Custom builds add it back with a password in "
        "cub-custom.example.yaml")


def test_the_custom_example_shows_how_to_secure_a_build_of_your_own():
    text = (_ESPHOME / "cub-custom.example.yaml").read_text()
    assert "encryption:" in text and "password:" in text, (
        "the custom example is where a builder learns to set the secrets a "
        "shared image cannot hold")


def test_the_cub_exposes_nothing_writable():
    """The unencrypted API is only defensible while it is read only. A switch,
    button, light or similar would turn LAN visibility into LAN control."""
    config = _base_config()
    writable = [k for k in ("switch", "button", "light", "fan", "number",
                            "select", "lock", "valve", "cover")
                if k in config]
    assert not writable, (
        f"the Cub gained writable entities {writable}; either encrypt the API "
        "or keep the firmware read only")


@pytest.mark.parametrize("name", [
    "pantry_raider.cpp", "pantry_raider.h", "automation.h", "cub_ble_parse.h",
    "__init__.py", "sensor.py", "text_sensor.py", "check_vectors.py",
])
def test_every_component_source_declares_gpl(name):
    head = (_COMPONENT / name).read_text()[:600]
    assert "SPDX-License-Identifier: GPL-3.0-or-later" in head, (
        f"{name} is compiled into a GPL-3.0 firmware image and must say so")


def test_the_gpl_text_and_notice_ship_with_the_source():
    copying = _COMPONENT / "COPYING"
    assert copying.is_file() and "GNU GENERAL PUBLIC LICENSE" in copying.read_text()[:400]
    notice = (_ESPHOME / "NOTICE.md").read_text()
    for needed in ("GPL-3.0", "github.com/Syracuse3DPrintingOrg/PantryRaider"):
        assert needed in notice, f"esphome/NOTICE.md must carry {needed}"


def test_the_repository_license_carves_the_firmware_out():
    """PolyForm forbids commercial use; GPL-3.0 forbids adding that kind of
    restriction. The root license has to say the firmware is not covered."""
    text = (_ROOT / "LICENSE").read_text()
    assert "esphome/NOTICE.md" in text and "GPL-3.0-or-later" in text


def test_the_notices_file_states_the_esphome_split_the_right_way_round():
    """It once said the opposite (GPL for the tooling, MIT for device code),
    which would tell a reader the firmware is permissively licensed."""
    text = (_ROOT / "THIRD_PARTY_NOTICES.md").read_text()
    line = next(ln for ln in text.splitlines() if "esphome.io" in ln)
    assert "GPL-3.0" in line and ".cpp" in line
    assert "MIT for the generated device code" not in line


def test_the_firmware_source_is_not_stripped_from_the_public_repository():
    """GPL-3.0 requires the corresponding source to be available, and the
    public repository is where NOTICE.md points."""
    strip = (_ROOT / "scripts" / "public-strip.txt").read_text().splitlines()
    assert not any(ln.strip().startswith("esphome") for ln in strip if not ln.strip().startswith("#"))
