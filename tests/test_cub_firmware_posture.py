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


def test_no_network_path_can_put_firmware_on_a_cub():
    """Neither half of the OTA story may ship (FoodAssistant-11smp).

    Push (platform: esphome) went first: a shared factory image cannot carry a
    password, so it would let anyone on the network run code on every Cub.
    Pull (platform: http_request plus its update entity) went second, because
    it reached the same place by a longer road. The Cub found its server over
    unauthenticated mDNS, took that server's word that pairing was approved,
    and then installed whatever firmware the server offered, unattended. Half
    a fix read like a settled question, which was worse than none.

    Firmware changes by flashing over USB from /ui/cubs, and no other way.
    """
    config = _base_config()
    assert "ota" not in config, (
        "cub-base.yaml declares an ota platform again. Neither push nor pull "
        "may ship while a Cub will install unsigned firmware from whatever "
        "server it happens to be talking to.")
    assert "update" not in config, (
        "the http_request update entity is back; it is the half that pulled "
        "and installed firmware on a six-hour timer with no user action")
    assert "update.perform" not in _BASE.read_text(), (
        "something automates an unattended firmware install again")


def test_the_shipped_cub_is_receive_only():
    """The transport that ships must be the one that cannot be talked into
    anything: no pairing, no key, no polling, nothing sent back."""
    config = _base_config()
    assert config["substitutions"]["pr_transport"] == "ble", (
        "the shipped transport must be ble (receive-only). lan and auto both "
        "pair, hold a key, and let the firmware check follow a discovered "
        "server, which is the chain in FoodAssistant-11smp.")
    hub = config["pantry_raider"]
    for key in ("server", "api_key", "ota_manifest_url"):
        assert key not in hub, (
            f"the hub block sets {key!r}; a receive-only Cub has no server, "
            "no key and no firmware URL")


def test_receive_only_mode_clears_a_previous_life():
    """Setting the transport is not enough on its own. load_key_() and
    load_server_() run before the transport is examined, so a Cub reflashed
    from an older LAN build still held a key and a remembered address, still
    counted as paired() (which is only "we hold some key"), and would still
    have let the firmware check follow that address. The component has to
    clear both, or the guarantee only holds for devices with no past."""
    src = (_ESPHOME / "components" / "pantry_raider" / "pantry_raider.cpp").read_text()
    block = src[src.index("if (this->transport_ == CUB_TRANSPORT_BLE) {"):]
    block = block[:block.index("#endif")]
    assert "api_key_.clear()" in block, "receive-only must drop any stored key"
    assert "server_.clear()" in block, "receive-only must drop any remembered server"


def test_wifi_loss_never_reboots_a_broadcast_cub():
    """A receive-only Cub takes its state off the air and may have no Wi-Fi
    credentials at all. ESPHome's default reboots a device that has not
    connected in fifteen minutes, which would be a permanent reboot loop."""
    assert _base_config()["wifi"].get("reboot_timeout") == "0s", (
        "cub-base.yaml must keep wifi reboot_timeout: 0s, or a Cub with no "
        "Wi-Fi reboots itself every fifteen minutes forever")


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
