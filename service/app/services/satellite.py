"""Pull-side of satellite config federation.

On a satellite (deployment_mode=pi_remote) this fetches the shareable backend
config from the main server and applies it to the live settings object, then
mirrors the expiry-defaults table into the local DB. The pulled fields are
recorded in ``settings.server_sourced_fields`` so the UI can show them
read-only: a satellite mirrors its server, it does not edit backend config.

The pull is best-effort: if the server is unreachable the satellite keeps
whatever it last had (or runs unconfigured) rather than crashing.
"""
from __future__ import annotations

import hashlib
import hmac
import json as _json
import logging
import secrets as _secrets
import socket
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

import httpx

from ..config import settings, SATELLITE_PULL_FIELDS, UNSIGNED_COSMETIC_FIELDS, APP_VERSION
from . import upstream
from .bridge import bridge_headers, invalidate_bridge_token
from .ttl_cache import TTLCache

logger = logging.getLogger("foodassistant.satellite")


# --------------------------------------------------------------------------
# Config-pull authentication (FoodAssistant-619i)
# --------------------------------------------------------------------------
#
# The satellite pulls backend config over plaintext HTTP, so on a flat LAN an
# attacker who answers on the server's IP (ARP/mDNS spoof, or a DHCP-reassigned
# address) could hand back poisoned config: redirect grocy_base_url/ollama to
# attacker endpoints, overwrite the HA token, etc. Full TLS with a pinned cert
# is out of scope here, so we make the server PROVE it holds the shared API key
# before the satellite will apply any credential-bearing field.
#
# The satellite sends a fresh random nonce with each pull. The server signs the
# config block it returns with HMAC-SHA256 keyed by the matched API key, over
# the nonce plus a canonical serialization of the config. The satellite recomputes
# the same HMAC with its own upstream_api_key and compares. A naive impostor that
# does not know the key cannot forge the signature, so it cannot push config; a
# tampered or replayed response fails the nonce/signature check too.
#
# Residual risk, documented honestly: the satellite still SENDS its API key in the
# request (X-API-Key), so an ACTIVE man-in-the-middle who captures that request
# learns the key and can then forge a valid signature. This scheme stops the
# passive/naive impostor and config poisoning; it does not defend key theft
# against an attacker already positioned to read the request. For that, run the
# fleet channel over a trusted LAN, a VPN, or TLS. See docs/security.md.

# Pulled fields that carry a credential or redirect a backend. These are applied
# ONLY when the server's response is signed with a valid key; an unsigned or
# badly-signed response never overwrites them.
_SENSITIVE_SUFFIXES = ("_api_key", "_token", "_base_url", "_public_url", "_url")
# Fields the suffix rule misses because the risky value lives INSIDE a list or
# dict (a URL, an action, a queue name) rather than in the field's own name, or
# whose value steers which backend/provider is used. These carry attacker value
# too, so they are signature-gated like the named credentials above.
_SENSITIVE_EXTRA = {
    "ai_extra_keys",
    # Camera list: each entry can carry a snapshot_url the app fetches
    # server-side, so an unsigned pull must not be able to add one.
    "streamdeck_cameras",
    # Custom Stream Deck keys: entries encode actions/macros (HA calls, URLs).
    "streamdeck_key_overrides",
    # Fleet printer defaults: a CUPS queue name that redirects where jobs go.
    "fleet_label_printer_queue", "fleet_document_printer_queue",
    # Backend/provider selectors: which store or LLM the satellite talks to.
    # Steering these only ever points at the signature-gated endpoints, but gate
    # them anyway so an unsigned pull cannot flip a satellite's backend.
    "vision_provider", "enrich_provider",
    "recipes_backend", "shopping_backend", "recipe_source",
    # Whether the server's Mealie is connected, which turns Mealie on here.
    "mealie_connected",
    # Home Assistant key map: each slot names an entity and the service the
    # deck calls on it.
    "streamdeck_ha_slots",
}


def _is_sensitive_field(name: str) -> bool:
    """True when a pulled field carries a secret, redirects a backend, or encodes
    an action/URL the app would act on. Name-based, plus an explicit set for
    fields whose risky value lives inside a list/dict (FoodAssistant-619i)."""
    return name.endswith(_SENSITIVE_SUFFIXES) or name in _SENSITIVE_EXTRA


def config_signature(api_key: str, nonce: str, config: dict) -> str:
    """Deterministic HMAC-SHA256 proving a config block came from a holder of
    ``api_key``. Both server and satellite compute it identically over the nonce
    and a canonical (sorted, compact) JSON of the config, so the satellite can
    verify without a shared secret beyond the API key it already holds. Pure."""
    canon = _json.dumps(config or {}, sort_keys=True, separators=(",", ":"),
                        default=str)
    msg = f"{nonce}\n{canon}".encode("utf-8")
    return hmac.new((api_key or "").encode("utf-8"), msg, hashlib.sha256).hexdigest()


def signature_ok(api_key: str, nonce: str, config: dict, presented: str) -> bool:
    """Constant-time check that ``presented`` is the right signature for this
    config under ``api_key`` and ``nonce``. False on any missing piece. Pure."""
    if not api_key or not nonce or not presented:
        return False
    expected = config_signature(api_key, nonce, config)
    # Bytes, so a non-ASCII value from an impostor is a mismatch, not a crash.
    return hmac.compare_digest(expected.encode("utf-8"), str(presented).encode("utf-8"))


# The parts of a pull reply that sit beside the signed config. From 0.21 the
# server puts their digest inside the config (extras_sha256), so the signature
# covers them too and a tampered profile list, alarm or command is caught.
EXTRAS_KEYS = ("server_hostname", "server_version", "expiry_defaults",
               "streamdeck_profiles", "gadget_config", "gadget_alarms", "command")


def extras_digest(extras: dict) -> str:
    """sha256 hex over the canonical JSON of the reply's extras. Pure; the
    server and the satellite call the same function."""
    extras = extras if isinstance(extras, dict) else {}
    canon = _json.dumps({k: extras.get(k) for k in EXTRAS_KEYS}, sort_keys=True,
                        separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# Credentials a relaying satellite leaves on its server (services/fleet_relay.py
# holds the same list for the server side; kept here so this module does not
# import it at load time).
SERVICE_FIELDS = (
    "gemini_api_key", "openai_api_key", "anthropic_api_key", "ollama_base_url",
    "themealdb_api_key", "spoonacular_api_key",
)

# The main server's version, learned on each successful sync. Process-local (a
# restart clears it until the next sync). Used by the auto-update scheduler so a
# satellite converges on its server's version rather than racing ahead to
# whatever is newest on GitHub (FoodAssistant-k2kk).
_last_server_version = ""


def last_server_version() -> str:
    """The server version seen on the most recent sync, or '' if not yet known."""
    return _last_server_version


def _record_last_sync(result: dict) -> None:
    """Persist a compact summary of a pull so the setup page can show its health.

    Stored under the ``satellite_last_sync`` setting (a small dict). Best-effort:
    a failure to persist must not turn an otherwise-successful sync into a crash.
    """
    summary = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": bool(result.get("ok")),
        "applied": list(result.get("applied", [])),
        "defaults": int(result.get("defaults", 0)),
        "error": result.get("error"),
    }
    try:
        settings.save({"satellite_last_sync": summary})
    except Exception as exc:
        logger.warning("satellite sync: could not record last-sync status: %s", exc)


def _local_ip() -> str:
    """Best-effort local IP for this device, '' if it cannot be determined.

    Opening a UDP socket toward a public address sends nothing but lets the OS
    pick the outbound interface, whose address we read back.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return ""


def _apply_config(config: dict, allow_sensitive: bool = True, relay: bool = False,
                  only=None) -> list[str]:
    """Overlay pulled backend config onto the live settings object.

    Returns the list of field names that were applied (server-sourced).

    When ``allow_sensitive`` is False (the server did not prove it holds the API
    key, see FoodAssistant-619i), credential-bearing and backend-redirecting
    fields are skipped: an unauthenticated response can refresh weather, theme,
    and the like, but it can never overwrite grocy_base_url, an AI key, or the HA
    token. A satellite keeps whatever verified backend config it last had.

    ``only`` limits the overlay to those fields (an unsigned reply applies
    UNSIGNED_COSMETIC_FIELDS and nothing else). ``relay`` is set when the
    server relays AI and recipe lookups for this device: their keys are then
    never taken, even when a reply carries them.

    The pulled values are persisted to settings.json, not just held in memory:
    a satellite often runs more than one worker process, and only the one that
    ran the sync would otherwise see the new config, so its setup page and camera
    page could show stale/empty values (e.g. a camera added on the server not
    appearing here). Persisting also means the last-known config survives a
    restart and is available before the first post-boot sync completes. These
    fields render read-only on a satellite, so persisting cannot be edited away.
    """
    if not isinstance(config, dict):
        # A malformed or hostile response (config as a string or list) must not
        # crash the sync: ``field in config`` would become a substring test and
        # ``config[field]`` would raise (FoodAssistant-619i). Nothing to apply.
        return []
    if "mealie_connected" not in config and "mealie_api_key" in config:
        # A server older than this device still sends its Mealie key instead
        # of the yes/no. Take the answer from it and leave the key itself
        # behind: this device reaches Mealie only through the server's proxy.
        config = dict(config, mealie_connected=bool(
            config.get("mealie_api_key") and config.get("mealie_base_url")))
    applied: list[str] = []
    persist: dict = {}
    allowed = set(only) if only is not None else None
    for field in SATELLITE_PULL_FIELDS:
        if field not in config:
            continue
        if allowed is not None and field not in allowed:
            continue
        if not allow_sensitive and _is_sensitive_field(field):
            continue  # never take a credential/backend URL from an unproven source
        if relay and field in SERVICE_FIELDS:
            continue  # the server runs these calls; its keys stay there
        object.__setattr__(settings, field, config[field])
        persist[field] = config[field]
        applied.append(field)
    if persist:
        try:
            settings.save(persist)
        except Exception as exc:  # non-fatal: the in-memory values still apply
            logger.warning("satellite sync: could not persist pulled config: %s", exc)
    # Record provenance so the UI can render these read-only. A partial
    # overlay (an unsigned reply) adds to what the last full pull recorded.
    sourced = set(applied)
    if allowed is not None:
        sourced |= set(getattr(settings, "server_sourced_fields", None) or ())
    object.__setattr__(settings, "server_sourced_fields", sourced)
    return applied


# The Pi host bridge, same address routers/setup proxies to. Used to push synced
# Stream Deck weather into the local config.toml (FoodAssistant-bra).
_HOST_BRIDGE = "http://127.0.0.1:9299"

# Settings fields that the Stream Deck controller mirrors from its config.toml.
# A subset of SATELLITE_PULL_FIELDS; when any are pulled we push them into the
# controller config so the deck matches the server: weather (bra) and the UI
# theme that recolours the keys (gxl).
_STREAMDECK_SYNCED_FIELDS = (
    "streamdeck_weather_location", "streamdeck_weather_units", "ui_theme",
    # key_style / icon_color are intentionally absent: they are a per-deck visual
    # choice owned by the device the deck is attached to, not pulled from the
    # server, so a satellite can set its own (FoodAssistant-ys79).
    # HA credentials/keys and cameras come from the server too, so a satellite's
    # deck drives the same entities and feeds without local setup (cr50).
    "streamdeck_ha_base_url", "streamdeck_ha_token", "streamdeck_ha_slots",
    "streamdeck_cameras",
    # Custom keys, so a button built on the server shows on every satellite deck
    # (FoodAssistant-n0r1).
    "streamdeck_key_overrides",
    # 12/24-hour clock reading, so a satellite's deck clock key follows the
    # server's clock_format like the kiosk and app pages do (FoodAssistant-ylax).
    "clock_format",
    # streamdeck_idle_timeout and streamdeck_logo_when_display_off are
    # intentionally absent, like key_style: the blank timeout and what this
    # deck shows while its own display sleeps are per-device concerns, edited
    # on the device itself. They are still written into config.toml by
    # _push_streamdeck_settings from this device's own settings.
)


def _merge_streamdeck_settings(config: dict, location: str, units: str, theme: str,
                               key_style: str = "rich", icon_color: str = "full",
                               ha_base_url: str = "", ha_token: str = "",
                               ha_slots: list | None = None,
                               cameras: list | None = None,
                               key_overrides: list | None = None,
                               idle_timeout_minutes: int = 0,
                               logo_when_display_off: bool = True,
                               rotation: int | None = None,
                               clock_format: str = "auto") -> dict:
    """Return config with the synced weather, theme, key style, HA, cameras,
    custom keys, idle timeout, and display-off logo choice overlaid.

    The bridge rewrites the whole config.toml from the posted dict, so a caller
    must read the current config, overlay just these keys, and post the whole
    thing back. Kept pure so the read-modify-write is unit-testable. A thin
    wrapper over services/deck_config.merge_deck_config in its legacy form
    (the token and camera URLs as given), with the Home Assistant keys this
    device's policy refuses left out.
    """
    from . import deck_config
    try:
        from . import ha_allowlist
        enforced = bool(ha_allowlist.policy_enforced(settings))
    except Exception:  # noqa: BLE001 - not there yet: enforce nothing extra
        enforced = False
    inputs = {
        "weather_location": location,
        "weather_units": units,
        "theme": theme,
        "key_style": key_style,
        "icon_color": icon_color,
        "ha_base_url": ha_base_url,
        "ha_token": ha_token,
        "ha_slots": list(ha_slots or []),
        "cameras": list(cameras or []),
        "key_overrides": list(key_overrides or []),
        "idle_timeout_minutes": idle_timeout_minutes,
        "logo_when_display_off": logo_when_display_off,
        "clock_format": clock_format,
        "is_satellite": True,
        "agent_key": "",
        "proxy_origin": "",
        "policy_enforced": enforced,
    }
    return deck_config.merge_deck_config(config, inputs, rotation=rotation)[0]


def _push_streamdeck_settings(timeout: float = 4.0) -> bool:
    """Mirror the synced settings into the local Stream Deck config.toml.

    Best-effort and only meaningful on a Pi appliance with a deck; see
    services/deck_config.push_to_deck, which this wraps. Never raises.
    """
    try:
        from . import deck_config
        return deck_config.push_to_deck(timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - leave the deck as it is
        logger.warning("satellite sync: could not push Stream Deck settings: %s",
                       type(exc).__name__)
        return False


def _pull_caps() -> str:
    """X-PR-Caps for the config pull: relay1, plus deckproxy1 once this
    satellite no longer uses the Home Assistant token itself.

    That needs its Stream Deck (if any) moved to the local proxy form, no
    Home Assistant thermometers of its own (those read Home Assistant with
    the local token), and no Bluetooth probe targets (their alerts go to Home
    Assistant with the local token too). Anything uncertain leaves deckproxy1
    off, which keeps the token coming."""
    caps = [upstream.RELAY_CAP]
    try:
        from . import deck_config
        deck_ok = (not settings.has_streamdeck) or deck_config.last_push_form() == "proxy"
        thermometers = False
        if settings.gadget_ha_enabled:
            from . import gadgets_ha
            thermometers = bool(gadgets_ha.configured_entities()
                                or gadgets_ha.configured_hygro_pairs())
        probe_targets = bool(settings.gadgets_enabled) and any(
            isinstance(d, dict) and d.get("targets")
            for d in (settings.gadget_devices or []))
        if deck_ok and not thermometers and not probe_targets:
            caps.append(upstream.DECK_PROXY_CAP)
    except Exception:  # noqa: BLE001 - the safe side: the token keeps flowing
        pass
    return ",".join(caps)


# This device's release-key fingerprint, read from the host bridge at most
# once an hour.
_trust_cache = TTLCache(3600.0)


def _device_trust() -> str:
    """The release-key fingerprint this Pi's updater trusts, or 'none'.

    Sent with the pull so the server can show which signing key each Bandit
    trusts. Best effort; never raises."""
    cached = _trust_cache.get()
    if cached is not None:
        return cached
    value = "none"
    try:
        if settings.is_pi_appliance():
            resp = httpx.get(f"{_HOST_BRIDGE}/update/state", headers=bridge_headers(),
                             timeout=1.5)
            if resp.status_code == 401:
                invalidate_bridge_token()
            elif resp.status_code == 200:
                body = resp.json()
                fp = body.get("trust_fp") if isinstance(body, dict) else ""
                value = str(fp or "none")[:120]
    except Exception:  # noqa: BLE001 - an unknown key reads as none
        value = "none"
    value = "".join(ch for ch in value if 32 <= ord(ch) < 127)[:120] or "none"
    _trust_cache.set(value)
    return value


# What a scrub pass may have to clear in older copies of settings.json.
_SNAPSHOT_DIR = ".pre-restore"
_scrub_warned: set = set()


def _scrub_file(path, fields, cameras) -> None:
    import os
    import tempfile
    from pathlib import Path
    path = Path(path)
    try:
        data = _json.loads(path.read_text())
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        data = None
    try:
        if not isinstance(data, dict):
            raise ValueError("not a settings object")
        changed = False
        for f in fields:
            if data.get(f):
                data[f] = ""
                changed = True
        if cameras is not None and "streamdeck_cameras" in data \
                and data.get("streamdeck_cameras") != cameras:
            data["streamdeck_cameras"] = cameras
            changed = True
        if not changed:
            return
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".scrub-", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(_json.dumps(data, indent=2))
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception:  # noqa: BLE001 - logged once per file, never raised
        if str(path) not in _scrub_warned:
            _scrub_warned.add(str(path))
            logger.warning("satellite sync: could not clear old copies of service keys in %s",
                           path.name)


def scrub_relayed_credentials(cfg: dict) -> list[str]:
    """Drop the service keys this satellite no longer needs, and their copies.

    Runs only after a verified reply carrying a relay block. Clears the AI and
    recipe keys (and any Grocy or Mealie key an older server handed out), plus
    the Home Assistant token when the server withheld it; then blanks the same
    fields in settings.json.bak and in the safety copies under .pre-restore.
    hosted_config_snapshot (this device's own settings from before it became a
    satellite) is left alone so switching back restores them. Returns the
    fields cleared from the live settings. Never raises; never logs values.
    """
    try:
        cfg = cfg if isinstance(cfg, dict) else {}
        candidates = list(SERVICE_FIELDS)
        parked = bool(getattr(settings, "hosted_stack_parked", False))
        snap = settings.hosted_config_snapshot if isinstance(
            settings.hosted_config_snapshot, dict) else {}
        for f in ("grocy_api_key", "mealie_api_key"):
            # On a device switched from hosting its own stack, a Grocy or
            # Mealie key the switch did not set aside is that stack's own
            # key, which Switch back needs.
            if parked and f not in snap:
                continue
            candidates.append(f)
        if "streamdeck_ha_token" not in cfg:
            candidates.append("streamdeck_ha_token")
        to_blank = [f for f in candidates if getattr(settings, f, "")]
        if to_blank:
            try:
                settings.save({f: "" for f in to_blank})
            except Exception as exc:  # noqa: BLE001 - still blank them in memory
                logger.warning("satellite sync: could not save cleared keys: %s",
                               type(exc).__name__)
                for f in to_blank:
                    object.__setattr__(settings, f, "")
            try:
                from ..dependencies import reset_providers
                reset_providers()
            except Exception:  # noqa: BLE001
                pass
        cams = settings.streamdeck_cameras
        sanitized = cams if (isinstance(cams, list) and any(
            isinstance(c, dict) and "relay" in c for c in cams)) else None
        # settings.save copies the previous file to settings.json.bak, so
        # this pass runs after it.
        from pathlib import Path
        data_dir = Path(settings.data_dir)
        files = [data_dir / "settings.json.bak", data_dir / "settings.json.last-good"]
        try:
            files += sorted((data_dir / _SNAPSHOT_DIR).glob("*/settings.json"))
        except OSError:
            pass
        if to_blank or sanitized is not None:
            for f in files:
                _scrub_file(f, to_blank, sanitized)
        return to_blank
    except Exception:  # noqa: BLE001 - a scrub never breaks a sync
        logger.warning("satellite sync: clearing old service keys failed")
        return []


def _push_timezone(tz: str, timeout: float = 4.0) -> bool:
    """Apply the fleet timezone to this satellite's host clock via the bridge.

    Best-effort and only meaningful on a Pi appliance: an empty value (the main
    server following its own system clock) is left alone. Never raises."""
    try:
        from ..hardware import is_raspberry_pi
        if not tz or not is_raspberry_pi():
            return False
        resp = httpx.post(f"{_HOST_BRIDGE}/system/timezone",
                          json={"tz": tz}, timeout=timeout,
                          headers=bridge_headers())
        if resp.status_code == 401:
            invalidate_bridge_token()
        return resp.status_code == 200
    except Exception as exc:  # bridge down / old: leave the host clock as-is
        logger.warning("satellite sync: could not push timezone: %s", exc)
        return False


def _push_update_channel(channel: str, timeout: float = 4.0) -> bool:
    """Mirror the fleet update channel to this satellite's host, where the OTA
    helper reads it (/etc/foodassistant/update-channel, FoodAssistant-wkwx).

    Best-effort and only meaningful on a Pi appliance. An old bridge without the
    /update/channel route answers 404; the update trigger re-pushes the channel,
    so the switch lands once the bridge has refreshed. Never raises."""
    try:
        from ..hardware import is_raspberry_pi
        if channel not in ("stable", "main") or not is_raspberry_pi():
            return False
        resp = httpx.post(f"{_HOST_BRIDGE}/update/channel",
                          json={"channel": channel}, timeout=timeout,
                          headers=bridge_headers())
        if resp.status_code == 401:
            invalidate_bridge_token()
        return resp.status_code == 200
    except Exception as exc:  # bridge down / old: leave the host file as-is
        logger.warning("satellite sync: could not push update channel: %s", exc)
        return False


def _apply_defaults(rows: list[dict]) -> int:
    """Replace the local expiry-defaults table with the server's copy.

    Deferred imports keep this module importable without a DB in tests that
    only exercise _apply_config.
    """
    from ..database import SessionLocal
    from ..models.db_models import ExpiryDefault

    db = SessionLocal()
    try:
        db.query(ExpiryDefault).delete()
        for r in rows:
            db.add(ExpiryDefault(
                category=r.get("category", ""),
                name_pattern=r.get("name_pattern", ""),
                storage_type=r.get("storage_type", ""),
                default_days=int(r.get("default_days", 0)),
                notes=r.get("notes"),
                priority=int(r.get("priority", 0)),
            ))
        db.commit()
        return len(rows)
    finally:
        db.close()


def _apply_profiles(rows: list[dict]) -> int:
    """Replace the local Stream Deck profiles table with the server's copy.

    Deferred imports keep this module importable without a DB in tests.
    """
    import json as _json
    from datetime import datetime, timezone
    from ..database import SessionLocal
    from ..models.db_models import StreamDeckProfile

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db = SessionLocal()
    try:
        db.query(StreamDeckProfile).delete()
        for r in rows:
            db.add(StreamDeckProfile(
                name=r.get("name", ""),
                deck_size=int(r.get("deck_size", 0)),
                key_overrides=_json.dumps(r.get("key_overrides", [])),
                created_at=r.get("updated_at", now),
                updated_at=r.get("updated_at", now),
            ))
        db.commit()
        return len(rows)
    finally:
        db.close()


def _apply_gadget_config(data: dict) -> None:
    """Mirror the server's gadget device lists for the relay (FoodAssistant-me3t).

    Stores the sanitized ``gadget_config`` block under the
    ``upstream_gadget_config`` setting, which GET /gadgets/config merges into
    the lists it hands the local Bluetooth reader, so a sensor added on the
    server is scanned for by this satellite too. Only applied when the server
    sent the block (an older server does not, and its absence must not wipe
    the last mirrored copy), and only persisted on change so a healthy sync
    does not rewrite settings.json every cycle. Never raises."""
    if not isinstance(data, dict) or "gadget_config" not in data:
        return
    try:
        from . import gadgets_relay
        mirrored = gadgets_relay.normalize_upstream_gadget_config(
            data.get("gadget_config"))
        if mirrored != (settings.upstream_gadget_config or {}):
            settings.save({"upstream_gadget_config": mirrored})
    except Exception as exc:  # noqa: BLE001 - the rest of the sync still counts
        logger.warning("satellite sync: could not mirror gadget config: %s", exc)


# --------------------------------------------------------------------------
# Protection alarms mirrored down from the server (FoodAssistant-me3t)
# --------------------------------------------------------------------------
#
# While a satellite relays its gadget readings, the main server owns the
# fridge/freezer/door alarms: it holds the thresholds and it fires the toast,
# so one breach never pages the kitchen twice. The catch is that the toast
# then only lands on the server's own screens, and the satellite is usually
# the one standing in the kitchen. So the server hands its live alarms back
# and the satellite replays them into its OWN on-screen event ring.
#
# The alarms ride two replies the satellite already receives, in this order of
# usefulness:
#
# 1. The relay's POST /gadgets/readings reply, which is the fast path: it
#    lands within seconds of the reading that raised the alarm, and needs no
#    new endpoint, no new poll, and no extra traffic.
# 2. The /api/config/satellite pull, which is the backstop: it is only every
#    satellite_sync_minutes, but it reaches a satellite with no reader of its
#    own (a plain kiosk), which never POSTs readings and would otherwise never
#    hear about the alarm at all.
#
# Both funnel into mirror_alarms(), which is deduped by alarm key and onset,
# so an alarm arriving on both paths still shows exactly one toast, matching
# the server's edge-triggered "one toast per onset" behavior.

def _alarm_state_file():
    from pathlib import Path
    from ..config import settings as _s
    return Path(_s.data_dir) / "satellite-alarms.json"


def select_new_alarms(alarms, seen: dict) -> tuple[list, dict]:
    """Split the server's live alarms into the ones not toasted here yet, and
    the seen-map to store next. Pure.

    ``seen`` maps an alarm key to the onset (started_epoch) already shown. An
    alarm is new when its key is unseen or its onset moved, so a condition
    that clears and comes back toasts again, and one that simply persists
    stays quiet. The returned map holds only currently-live alarms, so a
    cleared one is forgotten rather than accumulating forever."""
    seen = seen if isinstance(seen, dict) else {}
    fresh: list = []
    keep: dict = {}
    for alarm in alarms or []:
        if not isinstance(alarm, dict):
            continue
        key = str(alarm.get("key") or "").strip()
        if not key:
            continue
        try:
            started = int(alarm.get("started_epoch") or 0)
        except (TypeError, ValueError):
            started = 0
        keep[key] = started
        if seen.get(key) != started:
            fresh.append(alarm)
    return fresh, keep


def alarm_toast(alarm: dict) -> dict:
    """Shape one server alarm into the on-screen warning this device raises.
    Pure. The wording, level, and event key all come from the server, so the
    toast a satellite shows reads exactly like the one the server showed."""
    alarm = alarm if isinstance(alarm, dict) else {}
    level = alarm.get("level")
    return {
        "message": str(alarm.get("message") or "A sensor needs attention."),
        "title": str(alarm.get("location") or alarm.get("name")
                     or "Sensor alarm")[:40],
        "key": f"gadget-alarm:{alarm.get('key') or ''}",
        "level": level if level in ("warning", "error") else "warning",
    }


def mirror_alarms(alarms) -> int:
    """Replay the server's live protection alarms as local on-screen warnings.

    Returns how many toasts were raised (0 when nothing is new, which is the
    normal case). Only meaningful on a satellite. The seen-map lives in a
    small state file under data_dir, the same pattern as the other shared
    state, because a satellite can run more than one worker and each of them
    pulls: without a shared map every worker would toast the same alarm.
    Best-effort throughout, so a mirror problem never breaks a sync or a
    relay delivery."""
    import json as _json

    from ..config import settings as _s
    if not _s.is_satellite() or not alarms:
        return 0
    try:
        from . import ha_events
        from .state_lock import state_write_lock
        sf = _alarm_state_file()
        with state_write_lock(sf):
            try:
                seen = _json.loads(sf.read_text()).get("seen") or {}
            except (OSError, ValueError, TypeError, AttributeError):
                seen = {}
            fresh, keep = select_new_alarms(alarms, seen)
            if keep != seen:
                try:
                    from .. import durable
                    durable.write_json(sf, {"seen": keep}, keep_last_good=False)
                except OSError:
                    # data_dir is not writable: still toast, and accept that a
                    # restart may repeat one alarm rather than swallow it.
                    pass
        for alarm in fresh:
            ha_events.add_warning(timeout=0, **alarm_toast(alarm))
        return len(fresh)
    except Exception as exc:  # noqa: BLE001 - a mirror never breaks the caller
        logger.warning("satellite: could not mirror server alarms: %s", exc)
        return 0


def sync_from_upstream(timeout: float = 8.0) -> dict:
    """Pull backend config + defaults from the main server and apply them.

    Returns a small status dict: {"ok": bool, "applied": [...], "defaults": N,
    "command": str|None, "error": str|None}. Never raises on a network/HTTP
    failure. Each genuine pull attempt is recorded in the persisted
    ``satellite_last_sync`` setting so the setup page can show sync health.
    """
    result = _do_sync_from_upstream(timeout)
    # Skip the "not a satellite" guard: it is not a real pull attempt and only
    # fires in non-satellite or test contexts, so it should not overwrite the
    # last genuine sync status shown in the UI.
    if result.get("error") != "not a satellite":
        _record_last_sync(result)
    return result


def _swap_host(url: str, host: str) -> str:
    """Return url with its hostname replaced by host, keeping scheme/port/path.

    Used to retry the satellite sync against the server's cached IP when its
    .local name stops resolving. Pure, so it is unit-testable."""
    p = urlparse(url)
    port = f":{p.port}" if p.port else ""
    return urlunparse((p.scheme, f"{host}{port}", p.path, p.params, p.query, p.fragment))


def _is_ip_literal(host: str) -> bool:
    """True when host is already a bare IPv4/IPv6 address (no DNS needed)."""
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, host)
            return True
        except OSError:
            continue
    return False


def _resolve_host(host: str) -> str:
    """Resolve a hostname to an IPv4 address, or '' if it cannot be resolved."""
    if not host or _is_ip_literal(host):
        return host
    try:
        return socket.gethostbyname(host)
    except OSError:
        return ""


def _sync_candidates(url: str, host: str, cached_ip: str,
                     server_host: str = "") -> list[str]:
    """The URLs to try, in order, so the satellite survives both failure modes:

    1. Configured by mDNS name that stops resolving -> retry the cached server IP
       (FoodAssistant-xwn0).
    2. Configured by a bare IP that DHCP reassigns -> retry the server's
       advertised hostname as ``<host>.local`` (FoodAssistant-k9a8).

    The configured URL is always tried first; fallbacks are appended only when
    they differ from it, so a healthy setup makes exactly one request."""
    candidates = [url]
    cached_ip = (cached_ip or "").strip()
    if cached_ip and host and host != cached_ip and not _is_ip_literal(host):
        candidates.append(_swap_host(url, cached_ip))
    server_host = (server_host or "").strip().rstrip(".")
    if server_host:
        mdns = server_host if "." in server_host else f"{server_host}.local"
        if mdns and mdns != host:
            cand = _swap_host(url, mdns)
            if cand not in candidates:
                candidates.append(cand)
    return candidates


_extras_warned = {"done": False}


def _do_sync_from_upstream(timeout: float = 8.0) -> dict:
    if not settings.is_satellite():
        return {"ok": False, "error": "not a satellite", "applied": [], "defaults": 0, "command": None}
    base = (settings.remote_server_url or "").rstrip("/")
    if not base or not upstream.linked():
        return {"ok": False, "error": "missing server URL or API key", "applied": [], "defaults": 0, "command": None}

    url = f"{base}/api/config/satellite"
    # A fresh per-pull nonce the server signs its config with, so a captured
    # response cannot be replayed and an impostor cannot forge config it did not
    # sign with the shared key (FoodAssistant-619i).
    nonce = _secrets.token_hex(16)
    # Identity headers turn this pull into a heartbeat: the server records us in
    # its remotes list and may hand back a queued command in the response.
    headers = upstream.headers({
        "X-Device-Id": settings.device_id,
        "X-Device-Hostname": socket.gethostname(),
        "X-Device-Mode": settings.deployment_mode,
        "X-Device-Version": APP_VERSION,
        "X-Device-Ip": _local_ip(),
        "X-Config-Nonce": nonce,
        "X-Device-Trust": _device_trust(),
        "X-PR-Caps": _pull_caps(),
    })
    host = urlparse(base).hostname or ""
    resp = None
    last_exc = None
    for cand in _sync_candidates(url, host, settings.remote_server_ip,
                                 settings.remote_server_host):
        try:
            resp = httpx.get(cand, headers=headers, timeout=timeout)
            break
        except Exception as exc:  # try the next candidate (e.g. cached IP)
            last_exc = exc
            logger.warning("satellite sync: cannot reach %s: %s", cand, exc)
    if resp is None:  # every candidate failed: keep prior config
        return {"ok": False, "error": f"cannot reach server: {last_exc}", "applied": [], "defaults": 0, "command": None}

    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.json().get("detail", "")
        except Exception:
            detail = resp.text[:200]
        logger.warning("satellite sync: server returned %s: %s", resp.status_code, detail)
        return {"ok": False, "error": f"server {resp.status_code}: {detail}", "applied": [], "defaults": 0, "command": None}

    # Cache the server's current IP for use as a fallback the next time its
    # .local name does not resolve. Resolve the configured host while mDNS is
    # working (a literal IP resolves to itself); only persist when it changed,
    # so a healthy sync does not rewrite settings.json every cycle.
    fresh_ip = _resolve_host(host)
    if fresh_ip and fresh_ip != (settings.remote_server_ip or ""):
        try:
            settings.save({"remote_server_ip": fresh_ip})
        except Exception as exc:  # non-fatal: the fallback just stays stale
            logger.warning("satellite sync: could not cache server IP: %s", exc)

    try:
        data = resp.json()
    except Exception:
        data = None
    if not isinstance(data, dict):
        # A non-JSON or non-object body (an impostor, or a captive portal) must
        # not crash the sync (FoodAssistant-619i): keep the prior config.
        logger.warning("satellite sync: server returned a non-object body; ignoring")
        return {"ok": False, "error": "malformed server response",
                "applied": [], "defaults": 0, "command": None}
    # Verify the server proved it holds the shared API key before trusting
    # anything that matters (FoodAssistant-619i). A valid signature over our
    # nonce lets the full config through; a missing one (a server from before
    # the signed pull, or an unauthenticated impostor) changes only look and
    # feel; a present but wrong signature is a tamper/impostor and the whole
    # config is refused.
    cfg = data.get("config", {})
    sig = str(data.get("config_signature", "") or "")
    echoed_nonce = str(data.get("config_nonce", "") or "")
    verified = bool(sig) and echoed_nonce == nonce and signature_ok(
        upstream.signing_key(), nonce, cfg, sig)
    if sig and not verified:
        logger.warning("satellite sync: config signature did not verify "
                       "(tampered or wrong key); refusing this config")
        return {"ok": False, "error": "config signature invalid",
                "applied": [], "defaults": 0, "command": None}
    if not verified:
        logger.warning("satellite sync: server did not sign its config "
                       "(older server or unauthenticated source); applying "
                       "non-sensitive fields only, keeping prior backend config")
        applied = _apply_config(cfg, only=UNSIGNED_COSMETIC_FIELDS)
        # The deck shows the theme, weather and clock too, so it follows them.
        if any(f in applied for f in _STREAMDECK_SYNCED_FIELDS):
            _push_streamdeck_settings()
        return {"ok": True, "applied": applied, "defaults": 0, "command": None,
                "error": None}
    if not isinstance(cfg, dict):
        cfg = {}

    # The digest of everything beside the config, which the signature covers.
    # A server older than 0.21 sends none, and its extras apply as before.
    extras_ok = "extras_sha256" not in cfg or hmac.compare_digest(
        str(cfg.get("extras_sha256")).encode("utf-8"),
        extras_digest(data).encode("utf-8"))
    if not extras_ok and not _extras_warned["done"]:
        _extras_warned["done"] = True
        logger.warning("satellite sync: the server summary did not match its reply; "
                       "keeping the previous profiles, alarms and defaults")

    # The relay block: present only from a 0.21 server answering a relay pull.
    # Anything else (an older or rolled-back server) puts this device back on
    # its own credentials, and the next pull from a 0.21 server relays again.
    block = cfg.get("fleet_relay")
    relay_mode = isinstance(block, dict) and block.get("v") == 1
    new_block = dict(block) if relay_mode else {}
    hpv = cfg.get("ha_policy_version")
    new_hpv = hpv if isinstance(hpv, int) and not isinstance(hpv, bool) and hpv >= 0 else 0
    persist: dict = {}
    if new_block != (settings.fleet_relay or {}):
        persist["fleet_relay"] = new_block
    if new_hpv != int(settings.upstream_ha_policy_version or 0):
        persist["upstream_ha_policy_version"] = new_hpv
    if persist:
        try:
            settings.save(persist)
        except Exception as exc:  # noqa: BLE001 - the live values still apply
            logger.warning("satellite sync: could not save the relay state: %s",
                           type(exc).__name__)
            for k, v in persist.items():
                object.__setattr__(settings, k, v)

    applied = _apply_config(cfg, allow_sensitive=True, relay=relay_mode)
    cleared: list[str] = []
    if relay_mode:
        cleared = scrub_relayed_credentials(cfg)

    defaults_n = 0
    command = None
    if extras_ok:
        # Remember the server's version for the auto-update scheduler (k2kk).
        global _last_server_version
        _last_server_version = str(data.get("server_version", "") or "").strip()
        # Learn the server's hostname so a bare-IP satellite can fall back to
        # <host>.local after a DHCP IP change (k9a8). Persist only on change.
        server_host = str(data.get("server_hostname", "") or "").strip().rstrip(".")
        if server_host and server_host != (settings.remote_server_host or ""):
            try:
                settings.save({"remote_server_host": server_host})
            except Exception as exc:  # non-fatal: the fallback just stays unset
                logger.warning("satellite sync: could not cache server hostname: %s", exc)
        try:
            defaults_n = _apply_defaults(data.get("expiry_defaults", []))
        except Exception as exc:  # DB not ready or bad row: config still applied
            logger.warning("satellite sync: applied config but defaults failed: %s", exc)
        try:
            _apply_profiles(data.get("streamdeck_profiles", []))
        except Exception as exc:
            logger.warning("satellite sync: could not mirror Stream Deck profiles: %s", exc)
        _apply_gadget_config(data)
        # The server's live fridge/freezer/door alarms, replayed on this device's
        # own screen (see the mirror section above). The backstop path: the relay
        # reply usually gets here first, and the dedupe means only one toast shows.
        mirror_alarms(data.get("gadget_alarms"))
        command = data.get("command")

    # Pulling new provider keys/models must invalidate the cached provider.
    try:
        from ..dependencies import reset_providers
        reset_providers()
    except Exception:
        pass

    # If the pull changed what the Stream Deck shows (weather, theme, Home
    # Assistant keys, cameras), mirror it into the local controller config so
    # the deck matches the server (bra: weather, gxl: theme colours).
    if (any(f in applied for f in _STREAMDECK_SYNCED_FIELDS)
            or "streamdeck_ha_token" in cleared):
        _push_streamdeck_settings()

    # The fleet shares one timezone set on the main server; apply the pulled
    # value to this satellite's own host clock so its logs and kiosk read right.
    if "timezone" in applied:
        _push_timezone(settings.timezone)

    # The fleet shares one update channel too; land the pulled value on this
    # satellite's host so its own OTA helper follows it (wkwx).
    if "update_channel" in applied:
        _push_update_channel(settings.update_channel)

    if command == "resync":
        # The heartbeat already re-pulled config in this same request, so a
        # queued resync is satisfied just by us getting here. Nothing more to do.
        logger.info("satellite sync: server requested resync (already fulfilled by this pull)")
    elif command:
        # Unknown command from a newer server: ignore so old satellites keep working.
        logger.info("satellite sync: ignoring unknown command %r", command)

    logger.info("satellite sync: applied %d fields, %d defaults from %s",
                len(applied), defaults_n, base)
    return {"ok": True, "applied": applied, "defaults": defaults_n, "command": command, "error": None}
