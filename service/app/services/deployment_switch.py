"""Switching a Pi Hosted appliance to satellite duty and back.

A pi_hosted appliance runs the full local stack (Grocy, optionally Mealie) in
Docker. Sometimes the household later adds a bigger main server and wants the
appliance to become a plain satellite of it, without reflashing the SD card.
The flow here supports that one-image-fits-all switch (FoodAssistant-dzx9):

- Switch to satellite: the local backend containers (grocy/mealie/ollama) are
  stopped and stay stopped across reboots (compose ``restart: unless-stopped``
  does not resurrect a manually stopped container), the current backend config
  is snapshotted, and deployment_mode flips to pi_remote so the app starts
  pulling config from the main server. Nothing is deleted: the Grocy/Mealie
  data directories stay on the device.
- Switch back: the parked stack is started again, deployment_mode flips back to
  pi_hosted, and the snapshotted backend config (local Grocy URL and API key,
  Mealie, AI keys) is restored exactly as it was.

The container stop/start itself is done by the host bridge (root); this module
holds only the pure decision and settings-shape logic so it is unit-testable
without Docker or a bridge.
"""
from __future__ import annotations

from urllib.parse import urlparse

from ..config import SATELLITE_PULL_FIELDS


def validate_server_url(url: str) -> tuple[bool, str]:
    """Validate and normalize the main-server URL entered for the switch.

    Returns (True, normalized_url) or (False, user-facing error). Accepts only
    http/https URLs with a hostname; the trailing slash is stripped so the
    stored value matches what the satellite sync expects.
    """
    u = (url or "").strip().rstrip("/")
    if not u:
        return (False, "The main server URL is required.")
    parsed = urlparse(u)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return (False, "Enter a full URL, like http://192.168.1.10:9284.")
    return (True, u)


def can_switch_to_satellite(deployment_mode: str) -> tuple[bool, str]:
    """Whether this device may stand down its stack and become a satellite.

    Only a pi_hosted appliance qualifies: a server has no host bridge to drive
    the switch, and a device already in pi_remote has nothing to stand down.
    Returns (ok, user-facing error when not ok).
    """
    if deployment_mode == "pi_hosted":
        return (True, "")
    if deployment_mode == "pi_remote":
        return (False, "This device is already running as a satellite.")
    return (False, "Only a Pi Hosted appliance can switch to satellite mode.")


def can_switch_back(deployment_mode: str, stack_parked: bool) -> tuple[bool, str]:
    """Whether this device may re-enable its parked local stack.

    Requires that the device is currently a satellite AND that it got there via
    the switch above (stack_parked), so a device that was never pi_hosted (a
    flashed Pi Remote, no local stack on disk) is never touched.
    Returns (ok, user-facing error when not ok).
    """
    if deployment_mode != "pi_remote":
        return (False, "This device is not running as a satellite.")
    if not stack_parked:
        return (False, "This device has no parked local stack to return to. "
                       "It was set up as a satellite from the start.")
    return (True, "")


def hosted_snapshot(values: dict) -> dict:
    """The backend config to remember before the satellite sync overwrites it.

    Keeps exactly the fields a satellite pulls from its server
    (SATELLITE_PULL_FIELDS): those are the ones the sync will replace with the
    server's values, and the ones that must come back (local Grocy URL and API
    key, Mealie, AI keys, expiry tuning) when the device returns to hosting its
    own stack. Unknown keys in ``values`` are ignored.
    """
    return {k: values[k] for k in SATELLITE_PULL_FIELDS if k in values}


def satellite_switch_settings(server_url: str, api_key: str, snapshot: dict) -> dict:
    """The settings dict to persist when switching to satellite mode.

    Flips the mode, records the upstream link, marks the local stack as parked
    (which is what later allows the switch back), and stores the pre-switch
    backend snapshot so nothing about the local stack's config is lost.
    """
    return {
        "deployment_mode": "pi_remote",
        "remote_server_url": server_url,
        "upstream_api_key": api_key,
        "hosted_stack_parked": True,
        "hosted_config_snapshot": snapshot,
    }


def hosted_restore_settings(snapshot: dict) -> dict:
    """The settings dict to persist when switching back to the full stack.

    Restores every snapshotted backend field, flips the mode back, and clears
    the parked flag and the snapshot itself. The upstream link fields
    (remote_server_url, upstream_api_key) are deliberately kept: they are
    unused in pi_hosted mode and pre-fill the form if the device is ever
    switched again.
    """
    data = dict(snapshot or {})
    data.update({
        "deployment_mode": "pi_hosted",
        "hosted_stack_parked": False,
        "hosted_config_snapshot": {},
    })
    return data
