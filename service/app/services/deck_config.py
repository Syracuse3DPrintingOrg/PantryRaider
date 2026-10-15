"""The Stream Deck config: what the deck is told, and what the page may see.

The deck reads config.toml on the device, which the host bridge writes from
a posted dict. This module builds that dict from app settings
(merge_deck_config), pushes it through the bridge (push_to_deck) and keeps a
small record of the last push in data_dir/deck-push.json.

There are two forms. Once the deck has proven its agent key (it reached the
app with navorigin1), it gets the proxy form: Home Assistant goes through
this app's /api/proxy/ha with the deck's own agent key as the bearer, and
every camera that needs a login or a token goes through this app's camera
routes. The Home Assistant token and the camera logins then never reach
config.toml. Before that, the legacy form is the stamp earlier releases
wrote: the Home Assistant address and token and the camera URLs as saved.

Both forms run every Home Assistant key through the key policy for the
'deck' source, so a key the owner has not allowed arrives as a plain
default key and a refused slot arrives empty.

The Settings page reads the same config back (GET /setup/streamdeck/config)
only to show the grid, rotation, brightness and weather, so the secrets are
blanked before it leaves the server (redact_deck_config). The deck's own
app key is blanked too; the POST puts the stored one back.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading

from app import durable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from ..config import settings
from .cameras import is_credentialed_url
from .state_lock import state_write_lock

logger = logging.getLogger(__name__)

# Flags this module adds to the page's copy. They mean nothing to the deck, so
# the POST drops them before the config is written back.
PAGE_ONLY_KEYS = ("ha_token_set", "key_policy", "api_key_set")

PUSH_FILE = "deck-push.json"
_HOST_BRIDGE = "http://127.0.0.1:9299"
_ROTATIONS = (0, 90, 180, 270)

_mem: dict = {"path": None, "mtime": None, "data": None}
_mem_lock = threading.Lock()


# --- The page's copy ---------------------------------------------------------


def redact_deck_config(config: dict) -> dict:
    """A copy of a deck config with its secrets blanked.

    ``ha_token`` becomes "" and ``ha_token_set`` says whether one is stored, so
    the page can still show that a token is set. ``api_key`` (the deck's own
    app key) becomes "" the same way, with ``api_key_set``. Each camera whose
    snapshot URL carries a login or a token gets snapshot_url "" and
    ``relay: True``. Anything that is not a dict comes back unchanged.
    """
    if not isinstance(config, dict):
        return config
    out = copy.deepcopy(config)
    out["ha_token_set"] = bool(str(out.get("ha_token") or "").strip())
    out["ha_token"] = ""
    out["api_key_set"] = bool(str(out.get("api_key") or "").strip())
    if "api_key" in out:
        out["api_key"] = ""
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


# --- Inputs ------------------------------------------------------------------


def _key_sha256(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest() if key else ""


def _is_satellite() -> bool:
    try:
        return bool(settings.is_satellite())
    except Exception:  # noqa: BLE001
        return getattr(settings, "deployment_mode", "") == "pi_remote"


def _agent_key() -> str:
    try:
        from . import local_agents
        return local_agents.agent_key("streamdeck") or ""
    except Exception:  # noqa: BLE001 - no bridge token, no agent key
        return ""


def _proven_origin() -> str:
    try:
        from . import local_agents
        return str((local_agents.deck_proven() or {}).get("origin") or "")
    except Exception:  # noqa: BLE001
        return ""


def _policy_enforced() -> bool:
    try:
        from . import ha_allowlist
        return bool(ha_allowlist.policy_enforced(settings))
    except Exception:  # noqa: BLE001 - not there: enforce nothing extra
        return False


def deck_inputs() -> dict:
    """Everything merge_deck_config needs, read from settings and local state.

    proxy_origin is the origin the deck proved itself at; when that proof has
    aged out, the origin of the last push is kept as long as that push was the
    proxy form with the same agent key (the latch), so a quiet month never
    sends the deck back to a token it may no longer have.
    """
    agent_key = _agent_key()
    origin = _proven_origin()
    if not origin and agent_key:
        lp = last_push()
        if (lp and lp.get("form") == "proxy" and lp.get("origin")
                and lp.get("key_sha256") == _key_sha256(agent_key)):
            origin = str(lp.get("origin") or "")
    return {
        "weather_location": settings.streamdeck_weather_location,
        "weather_units": settings.streamdeck_weather_units,
        "theme": settings.ui_theme,
        "key_style": settings.streamdeck_key_style,
        "icon_color": settings.streamdeck_icon_color,
        "ha_base_url": settings.streamdeck_ha_base_url,
        "ha_token": settings.streamdeck_ha_token,
        "ha_slots": copy.deepcopy(list(settings.streamdeck_ha_slots or [])),
        "cameras": copy.deepcopy(list(settings.streamdeck_cameras or [])),
        "key_overrides": copy.deepcopy(list(settings.streamdeck_key_overrides or [])),
        "idle_timeout_minutes": int(settings.streamdeck_idle_timeout or 0),
        "logo_when_display_off": bool(settings.streamdeck_logo_when_display_off),
        "clock_format": settings.clock_format,
        "is_satellite": _is_satellite(),
        "agent_key": agent_key,
        "proxy_origin": origin.rstrip("/"),
        "policy_enforced": _policy_enforced(),
    }


# --- The merge ---------------------------------------------------------------


def _entry_owned_calls(entry, slots) -> list:
    """[(entity, service, owner)] for an override or slot, the way a press runs."""
    from . import ha_allowlist
    fn = getattr(ha_allowlist, "owned_calls", None)
    if fn is not None:
        return fn(entry, slots)
    return [(e, s, entry) for e, s in ha_allowlist.entry_calls(entry, slots)]


def _entry_permitted(entry, slots, enforce: bool) -> bool:
    from . import ha_policy
    for entity_id, service, owner in _entry_owned_calls(entry, slots):
        ok, _msg = ha_policy.permitted(entity_id, service, owner, "deck", enforce=enforce)
        if not ok:
            return False
    return True


def _filter_overrides(overrides, slots, enforce: bool) -> list:
    out = []
    for entry in overrides or []:
        if not isinstance(entry, dict):
            continue
        if _entry_permitted(entry, slots, enforce):
            out.append(copy.deepcopy(entry))
        else:
            out.append({"type": "default", "slot": entry.get("slot")})
    return out


def _filter_slots(slots, enforce: bool) -> list:
    src = list(slots or [])
    out = []
    for entry in src:
        if not isinstance(entry, dict):
            continue
        if _entry_permitted(entry, src, enforce):
            out.append(copy.deepcopy(entry))
        else:
            out.append({"entity_id": "", "service": "", "label": entry.get("label", "")})
    return out


def _needs_proxy(cam: dict) -> bool:
    from . import cameras
    try:
        if cameras.resolve_ha_entity(cam)[0]:
            return True
    except Exception:  # noqa: BLE001
        pass
    return bool(cameras.is_reolink(cam) or is_credentialed_url(cam.get("snapshot_url"))
                or cam.get("relay"))


def _proxy_cameras(cams, origin: str) -> list:
    out = []
    for i, cam in enumerate(cams or []):
        if not isinstance(cam, dict):
            continue
        name = str(cam.get("name", "") or "")
        if _needs_proxy(cam):
            url = f"{origin}/ui/camera/{i}/snapshot?name={quote(name, safe='')}"
            out.append({"name": name, "snapshot_url": url, "ha_entity": ""})
        else:
            out.append({"name": name, "snapshot_url": str(cam.get("snapshot_url", "") or ""),
                        "ha_entity": ""})
    return out


def _legacy_cameras(cams, is_satellite: bool) -> list:
    from . import cameras
    out = []
    for cam in cams or []:
        if not isinstance(cam, dict):
            continue
        if not is_satellite and cam.get("source") == "reolink":
            # A Reolink camera has no stored snapshot_url; compose the
            # credentialed one so the deck (which fetches on the LAN) can show it.
            snap = cameras.reolink_snapshot_from_entry(cam)
        else:
            snap = cam.get("snapshot_url", "")
        out.append({"name": cam.get("name", ""), "snapshot_url": snap,
                    "ha_entity": cam.get("ha_entity", "")})
    return out


def merge_deck_config(config: dict, inputs: dict, *, rotation=None) -> tuple:
    """(merged, form) for a deck config read from the bridge.

    Pure. The result starts from a copy of ``config`` (so the deck's own
    layout, brightness and URLs stay) and overlays what app settings own.
    ``api_key`` is never touched: the deck's agent key reaches it through its
    own drop-in. ``form`` is 'proxy' when the deck has an agent key and a
    proven origin, else 'legacy'.
    """
    merged = copy.deepcopy(config) if isinstance(config, dict) else {}
    inp = inputs or {}
    merged["theme"] = inp.get("theme")
    merged["key_style"] = inp.get("key_style")
    merged["icon_color"] = inp.get("icon_color")
    merged["clock_format"] = inp.get("clock_format") or "auto"
    try:
        merged["idle_timeout_minutes"] = max(0, int(inp.get("idle_timeout_minutes") or 0))
    except (TypeError, ValueError):
        merged["idle_timeout_minutes"] = 0
    merged["logo_when_display_off"] = bool(inp.get("logo_when_display_off"))
    if rotation is not None:
        try:
            r = int(rotation)
        except (TypeError, ValueError):
            r = None
        if r in _ROTATIONS:
            merged["rotation"] = r
    is_sat = bool(inp.get("is_satellite"))
    if is_sat:
        merged["weather_location"] = inp.get("weather_location")
        merged["weather_units"] = inp.get("weather_units")

    agent_key = str(inp.get("agent_key") or "")
    origin = str(inp.get("proxy_origin") or "").rstrip("/")
    form = "proxy" if (agent_key and origin) else "legacy"
    if form == "proxy":
        merged["ha_base_url"] = origin + "/api/proxy/ha"
        merged["ha_token"] = agent_key
        merged["cameras"] = _proxy_cameras(inp.get("cameras"), origin)
    else:
        merged["ha_base_url"] = inp.get("ha_base_url") or ""
        merged["ha_token"] = inp.get("ha_token") or ""
        merged["cameras"] = _legacy_cameras(inp.get("cameras"), is_sat)

    enforce = bool(inp.get("policy_enforced"))
    slots = list(inp.get("ha_slots") or [])
    merged["ha_slots"] = _filter_slots(slots, enforce)
    merged["key_overrides"] = _filter_overrides(inp.get("key_overrides"), slots, enforce)
    merged["ha_policy_version"] = 1 if enforce else 0
    return merged, form


# --- The push record (data_dir/deck-push.json) ----------------------------------


def _push_file() -> Path:
    return Path(settings.data_dir) / PUSH_FILE


def last_push() -> dict | None:
    """{'form', 'at', 'key_sha256', 'origin'} of the last push, or None."""
    pf = _push_file()
    with _mem_lock:
        if _mem["path"] != str(pf):
            _mem.update(path=str(pf), mtime=None, data=None)
        try:
            mtime = pf.stat().st_mtime_ns
        except OSError:
            data = _mem["data"]
            return dict(data) if isinstance(data, dict) else None
        if mtime != _mem["mtime"]:
            try:
                data = json.loads(pf.read_text())
                if isinstance(data, dict):
                    _mem["data"] = data
                    _mem["mtime"] = mtime
            except (OSError, ValueError):
                pass
        data = _mem["data"]
        return dict(data) if isinstance(data, dict) else None


def last_push_form() -> str | None:
    lp = last_push()
    form = (lp or {}).get("form")
    return form if form in ("proxy", "legacy") else None


def record_push(form: str, agent_key: str = "", origin: str = "") -> None:
    """Remember a push. Best effort: an unwritable data_dir keeps it in memory."""
    data = {
        "form": form,
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "key_sha256": _key_sha256(agent_key) if form == "proxy" else "",
        "origin": (origin or "") if form == "proxy" else "",
    }
    pf = _push_file()
    with _mem_lock:
        _mem["path"] = str(pf)
        _mem["data"] = data
    try:
        with state_write_lock(pf):
            mtime = durable.write_bytes_atomic(pf, json.dumps(data).encode())
            with _mem_lock:
                _mem["mtime"] = mtime
    except OSError:
        pass


def record_merged_push(form: str, inputs: dict) -> None:
    record_push(form, str((inputs or {}).get("agent_key") or ""),
                str((inputs or {}).get("proxy_origin") or ""))


# --- Pushing ------------------------------------------------------------------


def push_to_deck(timeout: float = 4.0) -> bool:
    """Bring the deck's config.toml up to date with app settings.

    Only on a Pi with a Stream Deck; anywhere else it returns False with no
    I/O. Reads the current config through the bridge and posts the merge only
    when something changed. A failed read never posts, so a broken read can
    never wipe the deck's layout. Never raises.
    """
    try:
        from ..hardware import is_raspberry_pi
        if not (is_raspberry_pi() and settings.has_streamdeck):
            return False
        import httpx
        from .bridge import bridge_headers, invalidate_bridge_token
        cur = httpx.get(f"{_HOST_BRIDGE}/streamdeck/config", timeout=timeout,
                        headers=bridge_headers())
        if cur.status_code != 200:
            if cur.status_code == 401:
                invalidate_bridge_token()
            logger.warning("Stream Deck config read failed (HTTP %s), not pushing",
                           cur.status_code)
            return False
        try:
            body = cur.json()
        except ValueError:
            body = None
        config = body.get("config") if isinstance(body, dict) else None
        if not isinstance(config, dict):
            logger.warning("Stream Deck config read returned no config, not pushing")
            return False
        inputs = deck_inputs()
        merged, form = merge_deck_config(
            config, inputs, rotation=(settings.streamdeck_rotation or None))
        if merged == config:
            record_merged_push(form, inputs)
            return True
        r = httpx.post(f"{_HOST_BRIDGE}/streamdeck/config", json={"config": merged},
                       timeout=timeout, headers=bridge_headers())
        if r.status_code == 401:
            invalidate_bridge_token()
        if r.status_code != 200:
            logger.warning("Stream Deck config write failed (HTTP %s)", r.status_code)
            return False
        record_merged_push(form, inputs)
        return True
    except Exception as e:  # noqa: BLE001 - best effort
        logger.warning("Stream Deck config push failed: %s", type(e).__name__)
        return False


async def apush_to_deck() -> bool:
    """push_to_deck off the event loop."""
    import anyio
    try:
        return await anyio.to_thread.run_sync(push_to_deck)
    except Exception:  # noqa: BLE001
        return False


# --- One-time cleanup -----------------------------------------------------------


def strip_ha_camera_token(cam):
    """(entry, blanked) for one saved camera: a Home Assistant camera without
    its token-carrying addresses.

    An entry with ha_entity loses any snapshot_url or stream_url that carries
    a token or login. An entry saved before cameras had ha_entity (its
    entity lives only in a camera_proxy address with ?token=) first gets
    ha_entity from that address, when this install has a Home Assistant
    address to reach it with, and then loses the address the same way. Such
    a camera already works through its entity, so nothing stops working.
    Anything else comes back unchanged with 0.
    """
    if not isinstance(cam, dict):
        return cam, 0
    from . import cameras
    new = dict(cam)
    entity = str(new.get("ha_entity") or "").strip()
    if not entity and str(settings.streamdeck_ha_base_url or "").strip():
        for field in ("snapshot_url", "stream_url"):
            value = str(new.get(field) or "")
            if not (value and is_credentialed_url(value)):
                continue
            try:
                found, _base = cameras.resolve_ha_entity({field: value})
            except Exception:  # noqa: BLE001
                found = ""
            if str(found or "").startswith("camera."):
                entity = str(found)
                break
        if entity:
            new["ha_entity"] = entity
    if not entity:
        return cam, 0
    blanked = 0
    for field in ("snapshot_url", "stream_url"):
        if new.get(field) and is_credentialed_url(new.get(field)):
            new[field] = ""
            blanked += 1
    return (new, blanked) if blanked else (cam, 0)


def scrub_ha_camera_token_urls() -> int:
    """Blank token-carrying URLs on saved Home Assistant cameras.

    Cameras added from Home Assistant before this release saved a snapshot and
    stream address with the long-lived token in it. The camera works through
    its entity, so those addresses are blanked and the token leaves settings
    and backups (strip_ha_camera_token). Returns how many addresses were
    blanked; a no-op once clean and on a satellite (its list comes from the
    main server). Never raises.
    """
    try:
        if _is_satellite():
            return 0
        cams = list(settings.streamdeck_cameras or [])
        changed = 0
        out = []
        for cam in cams:
            new, blanked = strip_ha_camera_token(cam)
            changed += blanked
            out.append(new)
        if changed:
            settings.save({"streamdeck_cameras": out})
            logger.info("Removed the Home Assistant token from %d saved camera addresses.",
                        changed)
        return changed
    except Exception:  # noqa: BLE001
        logger.debug("camera token scrub failed", exc_info=True)
        return 0
