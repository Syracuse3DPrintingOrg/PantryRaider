"""What the main server hands each satellite on its config pull (0.21).

A satellite from 0.21 on sends the ``relay1`` cap on every request: it asks
this server to run its AI, recipe-lookup, Home Assistant and camera calls
instead of holding those credentials itself. Such a satellite gets a payload
without the AI and recipe keys, plus a ``fleet_relay`` block that says which
relays exist and whether they can answer. An older satellite sends no cap and
keeps getting the credentials while the owner's policy allows it.

The builders here are pure and take their inputs as arguments, so the matrix
of satellite versions and policies is testable without a server. The route
(routers/satellite.py) feeds them settings and the device registry.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .ttl_cache import TTLCache

logger = logging.getLogger("foodassistant.fleet_relay")

# Credentials a relaying satellite never needs: the server runs these calls.
SERVICE_FIELDS = (
    "gemini_api_key", "openai_api_key", "anthropic_api_key", "ollama_base_url",
    "themealdb_api_key", "spoonacular_api_key",
)

# The Home Assistant token, which a satellite's Stream Deck still uses until
# it moves to the local proxy (the deckproxy1 cap).
DECK_TOKEN_FIELD = "streamdeck_ha_token"

# What a 0.21 server relays for its satellites.
RELAY_CAPS = ["ai", "recipes_external", "ha", "camera"]

# Fields the cut-off and relay payloads must never carry. Never pulled anyway.
_NEVER_SENT = ("grocy_api_key", "mealie_api_key")

# A heartbeat older than this does not count toward the auto policy.
RECENT_DAYS = 30

ADVISORY_KIND = "rotate_service_keys"
ADVISORY_DEDUPE = "rotate-service-keys-v1"
ADVISORY_TITLE = "Replace the AI and recipe keys your Bandits used to hold"
ADVISORY_BODY = (
    "Your Bandits now use this kitchen for AI and recipe lookups, so they no "
    "longer keep their own copies of those keys. Older copies can still sit in "
    "their backups. To be safe, create new keys with your AI and recipe "
    "providers, enter them in Settings, and delete the old ones there."
)
# The same advice when the owner turned older Bandits' keys off while some
# Bandit has not updated yet: that one has no AI until it does.
ADVISORY_BODY_OFF = (
    "Your Bandits no longer get copies of the AI and recipe keys from this "
    "kitchen. Older copies can still sit in their backups. To be safe, create "
    "new keys with your AI and recipe providers, enter them in Settings, and "
    "delete the old ones there. A Bandit that has not been updated cannot use "
    "AI or recipe lookups until it is."
)
ADVISORY_HA = (
    " For Home Assistant, update the Pantry Raider integration in HACS and "
    "approve its pairing here: it gives this kitchen a token of its own and "
    "removes the old one."
)
# Set after each check, so the registry is read at most once an hour.
_advisory_checked = TTLCache(3600.0)


def fleet_block(s) -> dict:
    """The relay block a relaying satellite stores as settings.fleet_relay."""
    try:
        provider = s.enrich_provider or s.vision_provider
        enrich = bool(s.provider_key(provider))
    except Exception:  # noqa: BLE001 - a block must always build
        enrich = False
    return {
        "v": 1,
        "caps": list(RELAY_CAPS),
        "ai_available": bool(s.ai_configured()),
        "enrich_available": enrich,
        "ha_available": bool(s.ha_connected()),
        "legacy_credentials": str(getattr(s, "satellite_legacy_credentials", "auto") or "auto"),
        "server_has_password": bool(getattr(s, "auth_password", "")),
    }


def _parse_seen(value):
    if not value:
        return None
    try:
        seen = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return seen


def _row_caps(row: dict) -> set:
    caps = row.get("caps")
    if isinstance(caps, str):
        caps = caps.split(",")
    return {str(c) for c in (caps or []) if c}


def recent_satellites(rows, now=None) -> list[dict]:
    """Heartbeat rows from pi_remote devices seen in the last 30 days. Pure."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RECENT_DAYS)
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if row.get("source") != "heartbeat" or row.get("deployment_mode") != "pi_remote":
            continue
        seen = _parse_seen(row.get("last_seen"))
        if seen is None or seen < cutoff:
            continue
        out.append(row)
    return out


def policy_from(setting, rows, kind: str, now=None) -> str:
    """'allow' or 'off' for older satellites' credentials. Pure.

    'allow' and 'off' are the owner's literal choice. Anything else is auto:
    credentials keep flowing until every satellite seen in 30 days has
    updated past needing them (relay1 for the service keys, deckproxy1 for
    the Home Assistant token and the full camera list)."""
    if setting == "allow":
        return "allow"
    if setting == "off":
        return "off"
    recent = recent_satellites(rows, now)
    if not recent:
        return "allow"
    need = "deckproxy1" if kind == "deck" else "relay1"
    if all(need in _row_caps(r) for r in recent):
        return "off"
    return "allow"


def _list_devices() -> list[dict]:
    from . import devices
    try:
        return devices.list_devices()
    except Exception:  # noqa: BLE001 - no registry yet: no satellites seen
        logger.debug("device registry read failed", exc_info=True)
        return []


def effective_policy(kind: str) -> str:
    """policy_from over this server's setting and device registry."""
    from ..config import settings
    return policy_from(getattr(settings, "satellite_legacy_credentials", "auto"),
                       _list_devices(), kind)


def _sanitized_cameras(cameras) -> list:
    from . import cameras as cam
    return [cam.sanitize_camera_for_satellite(c) if isinstance(c, dict) else {}
            for c in (cameras if isinstance(cameras, list) else [])]


def build_satellite_config(values: dict, *, relay: bool, deck_proxy: bool,
                           service_legacy: bool, deck_legacy: bool,
                           block: dict | None) -> dict:
    """The config one satellite receives. Pure.

    relay: the satellite sent relay1, so the AI and recipe keys stay here and
    the relay block goes along. The Home Assistant token and the full camera
    list still go while its Stream Deck needs them (no deckproxy1 and the
    deck policy allows it); otherwise the token is left out and the cameras
    lose their logins (same length and order, so indexes line up).

    Not relay: an older satellite. The service keys go while service_legacy
    holds, else each is ''. The token and cameras follow deck_legacy the same
    way, except that a withheld token is sent as '' so the old satellite
    actually drops it.
    """
    cfg = dict(values or {})
    for f in _NEVER_SENT:
        cfg.pop(f, None)
    if relay:
        for f in SERVICE_FIELDS:
            cfg.pop(f, None)
        if deck_proxy or not deck_legacy:
            cfg.pop(DECK_TOKEN_FIELD, None)
            if "streamdeck_cameras" in cfg:
                cfg["streamdeck_cameras"] = _sanitized_cameras(cfg["streamdeck_cameras"])
        cfg["fleet_relay"] = dict(block or {})
        return cfg
    if not service_legacy:
        for f in SERVICE_FIELDS:
            if f in cfg:
                cfg[f] = ""
    if not deck_legacy:
        if DECK_TOKEN_FIELD in cfg:
            cfg[DECK_TOKEN_FIELD] = ""
        if "streamdeck_cameras" in cfg:
            cfg["streamdeck_cameras"] = _sanitized_cameras(cfg["streamdeck_cameras"])
    return cfg


def advisory_body(s, all_relay: bool = True) -> str:
    """The advisory text. all_relay: every recent Bandit runs its AI and
    recipe lookups through this kitchen (sends relay1)."""
    body = ADVISORY_BODY if all_relay else ADVISORY_BODY_OFF
    if getattr(s, "streamdeck_ha_token", "") and getattr(s, "ha_token_kind", "") != "dedicated":
        body += ADVISORY_HA
    return body


def maybe_raise_rotation_advisory(db, now: float | None = None) -> bool:
    """Once every Bandit relays, suggest replacing the keys they used to hold.

    One inbox item (an archived one stays archived), checked at most once an
    hour per process. Returns True when the item was created or refreshed.
    Never raises."""
    if _advisory_checked.get(now):
        return False
    _advisory_checked.set(True, now)
    try:
        from ..config import settings
        if getattr(settings, "satellite_legacy_credentials", "auto") == "allow":
            return False
        rows = _list_devices()
        if not any(isinstance(r, dict) and r.get("source") == "heartbeat"
                   and r.get("deployment_mode") == "pi_remote" for r in rows):
            return False
        if policy_from(settings.satellite_legacy_credentials, rows, "service") != "off":
            return False
        if not any(getattr(settings, f, "") for f in SERVICE_FIELDS):
            return False
        # Under "off" the item is raised even while a Bandit still lacks
        # relay1; the text must not claim that Bandit uses this kitchen.
        all_relay = policy_from("auto", rows, "service") == "off"
        from . import action_items
        action_items.create(db, ADVISORY_KIND, ADVISORY_TITLE,
                            body=advisory_body(settings, all_relay),
                            dedupe_key=ADVISORY_DEDUPE, level="info")
        return True
    except Exception:  # noqa: BLE001 - an advisory never breaks a pull
        logger.debug("rotation advisory check failed", exc_info=True)
        return False


def reset_advisory_check() -> None:
    """Forget the hourly throttle (tests)."""
    _advisory_checked.invalidate()
