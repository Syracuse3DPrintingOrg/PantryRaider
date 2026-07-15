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

import logging
import socket
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

import httpx

from ..config import settings, SATELLITE_PULL_FIELDS, APP_VERSION
from .bridge import bridge_headers, invalidate_bridge_token

logger = logging.getLogger("foodassistant.satellite")

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


def _apply_config(config: dict) -> list[str]:
    """Overlay pulled backend config onto the live settings object.

    Returns the list of field names that were applied (server-sourced).

    The pulled values are persisted to settings.json, not just held in memory:
    a satellite often runs more than one worker process, and only the one that
    ran the sync would otherwise see the new config, so its setup page and camera
    page could show stale/empty values (e.g. a camera added on the server not
    appearing here). Persisting also means the last-known config survives a
    restart and is available before the first post-boot sync completes. These
    fields render read-only on a satellite, so persisting cannot be edited away.
    """
    applied: list[str] = []
    persist: dict = {}
    for field in SATELLITE_PULL_FIELDS:
        if field not in config:
            continue
        object.__setattr__(settings, field, config[field])
        persist[field] = config[field]
        applied.append(field)
    if persist:
        try:
            settings.save(persist)
        except Exception as exc:  # non-fatal: the in-memory values still apply
            logger.warning("satellite sync: could not persist pulled config: %s", exc)
    # Record provenance so the UI can render these read-only.
    object.__setattr__(settings, "server_sourced_fields", set(applied))
    return applied


# The Pi host bridge, same address setup.py proxies to. Used to push synced
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
    thing back. Kept pure so the read-modify-write is unit-testable.
    """
    merged = dict(config)
    merged["weather_location"] = location
    merged["weather_units"] = units
    merged["theme"] = theme
    merged["key_style"] = key_style
    merged["icon_color"] = icon_color
    merged["ha_base_url"] = ha_base_url
    merged["ha_token"] = ha_token
    merged["ha_slots"] = ha_slots or []
    merged["cameras"] = [
        {"name": c.get("name", ""), "snapshot_url": c.get("snapshot_url", ""),
         "ha_entity": c.get("ha_entity", "")}
        for c in (cameras or []) if isinstance(c, dict)
    ]
    # Custom keys are applied to the deck by slot, so overlaying them is enough
    # for a server-built button to appear on the satellite (FoodAssistant-n0r1).
    merged["key_overrides"] = [o for o in (key_overrides or []) if isinstance(o, dict)]
    # Device-local fields the controller still only learns via config.toml: the
    # idle blank timeout (saved in app settings but never written to the deck
    # before, so it never blanked, FoodAssistant-3fdq) and whether the keys
    # show the logo while the display sleeps.
    merged["idle_timeout_minutes"] = max(0, int(idle_timeout_minutes or 0))
    merged["logo_when_display_off"] = bool(logo_when_display_off)
    # 12/24-hour clock reading rides with the timezone (a fleet-wide "how does a
    # time read" choice), so a satellite's deck clock key matches the server
    # (ylax). The controller falls back to 24-hour when this is absent.
    merged["clock_format"] = clock_format or "auto"
    # Deck rotation is a device-local app setting too (FoodAssistant-kl5n): a
    # None keeps whatever the config already had, so an unset value never
    # clobbers a rotation set directly in config.toml, while an explicit value
    # (e.g. from a hardware preset) is written through. Only the four supported
    # angles are honoured.
    if rotation is not None and int(rotation) in (0, 90, 180, 270):
        merged["rotation"] = int(rotation)
    return merged


def _push_streamdeck_settings(timeout: float = 4.0) -> bool:
    """Mirror the synced weather + theme into the local Stream Deck config.toml.

    Best-effort and only meaningful on a Pi appliance with a deck: reads the
    current bridge config, overlays the weather and theme fields from settings,
    and posts the merged config back so the running controller (which watches
    config.toml) picks up the server's values without a manual save. Returns
    True on a successful write, False on any error or when not applicable.
    Never raises.
    """
    try:
        from ..hardware import is_raspberry_pi
        if not (is_raspberry_pi() and settings.has_streamdeck):
            return False
        cur = httpx.get(f"{_HOST_BRIDGE}/streamdeck/config", timeout=timeout,
                        headers=bridge_headers())
        config = (cur.json() or {}).get("config", {}) if cur.status_code == 200 else {}
        merged = _merge_streamdeck_settings(
            config,
            settings.streamdeck_weather_location,
            settings.streamdeck_weather_units,
            settings.ui_theme,
            settings.streamdeck_key_style,
            settings.streamdeck_icon_color,
            settings.streamdeck_ha_base_url,
            settings.streamdeck_ha_token,
            settings.streamdeck_ha_slots,
            settings.streamdeck_cameras,
            settings.streamdeck_key_overrides,
            settings.streamdeck_idle_timeout,
            settings.streamdeck_logo_when_display_off,
            # A default 0 keeps whatever rotation the deck's config.toml already
            # has, so this background mirror never resets a rotation set directly
            # on the device; a preset's non-zero value writes through
            # (FoodAssistant-kl5n).
            settings.streamdeck_rotation or None,
            settings.clock_format,
        )
        resp = httpx.post(
            f"{_HOST_BRIDGE}/streamdeck/config", json={"config": merged}, timeout=timeout,
            headers=bridge_headers(),
        )
        if resp.status_code == 401:
            invalidate_bridge_token()
        return resp.status_code == 200
    except Exception as exc:  # bridge down, not a Pi, etc.: leave the deck as-is
        logger.warning("satellite sync: could not push Stream Deck settings: %s", exc)
        return False


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
                          json={"channel": channel}, timeout=timeout)
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
                    tmp = sf.with_name(sf.name + ".tmp")
                    tmp.write_text(_json.dumps({"seen": keep}))
                    import os as _os
                    _os.replace(tmp, sf)
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


def _do_sync_from_upstream(timeout: float = 8.0) -> dict:
    if not settings.is_satellite():
        return {"ok": False, "error": "not a satellite", "applied": [], "defaults": 0, "command": None}
    base = (settings.remote_server_url or "").rstrip("/")
    if not base or not settings.upstream_api_key:
        return {"ok": False, "error": "missing server URL or API key", "applied": [], "defaults": 0, "command": None}

    url = f"{base}/api/config/satellite"
    # Identity headers turn this pull into a heartbeat: the server records us in
    # its remotes list and may hand back a queued command in the response.
    headers = {
        "X-API-Key": settings.upstream_api_key,
        "X-Device-Id": settings.device_id,
        "X-Device-Hostname": socket.gethostname(),
        "X-Device-Mode": settings.deployment_mode,
        "X-Device-Version": APP_VERSION,
        "X-Device-Ip": _local_ip(),
    }
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

    data = resp.json()
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
    applied = _apply_config(data.get("config", {}))
    defaults_n = 0
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

    # Pulling new provider keys/models must invalidate the cached provider.
    try:
        from ..dependencies import reset_providers
        reset_providers()
    except Exception:
        pass

    # If the pull changed the Stream Deck weather or the UI theme, mirror it
    # into the local controller config.toml so the deck matches the server
    # (bra: weather, gxl: theme colours).
    if any(f in applied for f in _STREAMDECK_SYNCED_FIELDS):
        _push_streamdeck_settings()

    # The fleet shares one timezone set on the main server; apply the pulled
    # value to this satellite's own host clock so its logs and kiosk read right.
    if "timezone" in applied:
        _push_timezone(settings.timezone)

    # The fleet shares one update channel too; land the pulled value on this
    # satellite's host so its own OTA helper follows it (wkwx).
    if "update_channel" in applied:
        _push_update_channel(settings.update_channel)

    command = data.get("command")
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
