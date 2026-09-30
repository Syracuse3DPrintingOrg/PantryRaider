"""The Stream Deck config as the Settings page may see it.

The deck's config.toml holds the Home Assistant token and, for a camera with a
login, a snapshot URL with that login in it: the deck fetches on the LAN and
needs both. The Settings page reads the same config back (GET
/setup/streamdeck/config) only to show the grid, rotation, brightness and
weather, so the secrets are blanked before it leaves the server. Saving is
unaffected: the POST re-stamps the token and the camera list from app settings
whatever the page sends, so a page that posts back the blanked values loses
nothing.
"""
from __future__ import annotations

import copy

from .cameras import is_credentialed_url

# Flags this module adds to the page's copy. They mean nothing to the deck, so
# the POST drops them before the config is written back.
PAGE_ONLY_KEYS = ("ha_token_set",)


def redact_deck_config(config: dict) -> dict:
    """A copy of a deck config with its secrets blanked.

    ``ha_token`` becomes "" and ``ha_token_set`` says whether one is stored, so
    the page can still show that a token is set. Each camera whose snapshot
    URL carries a login or a token gets snapshot_url "" and ``relay: True``.
    Anything that is not a dict comes back unchanged.
    """
    if not isinstance(config, dict):
        return config
    out = copy.deepcopy(config)
    out["ha_token_set"] = bool(str(out.get("ha_token") or "").strip())
    out["ha_token"] = ""
    cams = out.get("cameras")
    if isinstance(cams, list):
        for cam in cams:
            if isinstance(cam, dict) and is_credentialed_url(cam.get("snapshot_url")):
                cam["snapshot_url"] = ""
                cam["relay"] = True
    return out


def strip_page_only_keys(config: dict) -> dict:
    """Drop the page-only flags from a posted config before it is written."""
    if isinstance(config, dict):
        for k in PAGE_ONLY_KEYS:
            config.pop(k, None)
    return config
