import asyncio
import fcntl
import json
import os
import re
import select
import struct
import threading
from pathlib import Path
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from ..config import (
    settings, APP_VERSION, THEMES, _DEFAULT_THEME,
    UI_SCALES, _DEFAULT_UI_SCALE,
    DISPLAY_ROTATIONS, _DEFAULT_DISPLAY_ROTATION,
    DISPLAY_TYPES, _DEFAULT_DISPLAY_TYPE,
    FLOATING_NAV_POSITIONS,
    FLOATING_NAV_ORIENTATIONS,
    STREAMDECK_KEY_STYLES, STREAMDECK_ICON_COLORS,
    DEPLOYMENT_MODES, _DEFAULT_DEPLOYMENT_MODE,
    AI_MODELS, SATELLITE_PULL_FIELDS,
    KITCHEN_APPLIANCES, KITCHEN_APPLIANCE_KEYS,
    browser_host, device_hostname,
)
from ..database import SessionLocal
from ..dependencies import reset_providers
from ..hardware import is_raspberry_pi, board_model
from ..models.db_models import StreamDeckProfile
from ..navigation import all_tabs
from ..storage_categories import custom_categories, _normalize_custom, storable
from ..templating import templates

router = APIRouter(prefix="/setup", tags=["setup"])

# Saved values for these are never rendered back into the page. The form
# sends "" to keep the stored value and "__CLEAR__" to erase it.
_SECRET_FIELDS = [
    "gemini_api_key", "openai_api_key", "anthropic_api_key",
    "grocy_api_key", "mealie_api_key",
    "themealdb_api_key", "spoonacular_api_key",
    "auth_password", "api_key", "upstream_api_key", "kiosk_pin",
    "streamdeck_ha_token",
]
_CLEAR = "__CLEAR__"


# Extra-key rows that were left untouched in the UI come back as this sentinel
# instead of the real (masked) value, so saved keys are never echoed to the
# browser. "__KEEP__:2" means "keep the stored extra key at index 2".
_KEEP_PREFIX = "__KEEP__:"


def _merge_satellite_keys(submitted) -> tuple[list[str], list[str]] | None:
    """Resolve submitted satellite extra-key rows against stored keys.

    Each row is a {"key": ..., "name": ...} object (a bare string is still
    accepted for backward compatibility). The key may be a real secret or a
    __KEEP__:<index> placeholder resolved to the stored key at that position.

    Returns aligned (keys, names) lists, or None when nothing was submitted
    (caller leaves stored extras untouched). Blanks and duplicate keys are
    dropped; the name follows its key.
    """
    if not isinstance(submitted, list):
        return None
    prev = [k for k in (settings.extra_api_keys if isinstance(settings.extra_api_keys, list) else []) if k]
    prev_names = settings.extra_api_key_names if isinstance(settings.extra_api_key_names, list) else []
    keys: list[str] = []
    names: list[str] = []
    for row in submitted:
        if isinstance(row, str):
            raw, name = row, ""
        elif isinstance(row, dict):
            raw, name = str(row.get("key", "")), str(row.get("name", "")).strip()
        else:
            continue
        val = raw.strip()
        if val.startswith(_KEEP_PREFIX):
            try:
                idx = int(val[len(_KEEP_PREFIX):])
                val = prev[idx]
                # An untouched saved row keeps its stored name unless renamed.
                if not name and idx < len(prev_names):
                    name = prev_names[idx]
            except (ValueError, IndexError):
                continue
        if val and val not in keys:
            keys.append(val)
            names.append(name)
    return keys, names


def _merge_extra_keys(submitted) -> dict | None:
    """Resolve the submitted extra-key map against what is already stored.

    Returns a clean {provider: [keys]} dict, or None when nothing was sent
    (so the caller leaves the stored extras untouched). Placeholders of the
    form "__KEEP__:<index>" are replaced with the matching stored key; blanks
    and duplicates are dropped.
    """
    if not isinstance(submitted, dict):
        return None
    stored = settings.ai_extra_keys if isinstance(settings.ai_extra_keys, dict) else {}
    result: dict = {}
    for provider, rows in submitted.items():
        if not isinstance(rows, list):
            continue
        prev = [k for k in stored.get(provider, []) if isinstance(k, str)]
        clean: list[str] = []
        for row in rows:
            if not isinstance(row, str):
                continue
            val = row.strip()
            if val.startswith(_KEEP_PREFIX):
                try:
                    val = prev[int(val[len(_KEEP_PREFIX):])]
                except (ValueError, IndexError):
                    continue
            if val and val not in clean:
                clean.append(val)
        if clean:
            result[provider] = clean
    return result


def _safe_error(e: Exception | str, *secrets: str) -> str:
    """Error text with any known secrets blanked (URLs can embed API keys)."""
    msg = str(e)
    for s in secrets:
        if s:
            msg = msg.replace(s, "•••")
    return msg


class SetupPayload(BaseModel):
    vision_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    ollama_base_url: str = ""
    ollama_model: str = "llava:7b"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-8"
    # Extra API keys per provider, e.g. {"gemini": ["key2", "key3"]}. When
    # absent the stored extras are left untouched (see save handler).
    ai_extra_keys: dict[str, list[str]] | None = None
    scanner_type: str = ""
    barcode_global_capture: bool = True
    barcode_enrichment: str = "llm"
    enrich_provider: str = ""
    enrich_model: str = ""
    grocy_base_url: str = ""
    grocy_api_key: str = ""
    grocy_public_url: str = ""
    device_hostname: str = ""
    mealie_base_url: str = ""
    mealie_api_key: str = ""
    mealie_public_url: str = ""
    recipe_source: str = "themealdb"
    themealdb_api_key: str = ""
    spoonacular_api_key: str = ""
    staple_items: str = ""
    perishable_days: int = 14
    expiring_soon_days: int = 5
    suggest_per_tier: int = 8
    nav_order: str = ""
    nav_hidden: str = ""
    ui_theme: str = _DEFAULT_THEME
    # Custom theme builder swatches (FoodAssistant-hatd). Declared so they
    # round-trip through /save (BaseModel drops undeclared fields).
    custom_theme_base: str = "dark"
    custom_theme_primary: str = "#4f9dff"
    custom_theme_accent: str = "#34d399"
    custom_theme_bg: str = "#0d1117"
    custom_theme_surface: str = "#161b22"
    custom_theme_text: str = "#e6edf3"
    ui_scale: str = _DEFAULT_UI_SCALE
    display_rotation: int = _DEFAULT_DISPLAY_ROTATION
    display_type: str = _DEFAULT_DISPLAY_TYPE
    deployment_mode: str = ""
    remote_server_url: str = ""
    upstream_api_key: str = ""
    kiosk_pin: str = ""
    barcode_llm_fallback: bool = False
    barcode_autocheck_shopping: bool = False
    cook_ai_context: str = ""
    # Kitchen appliances the user owns (list of catalog ids). None = field not
    # submitted (leave the stored selection alone); [] = explicitly none.
    kitchen_appliances: list[str] | None = None
    has_streamdeck: bool = False
    streamdeck_key_count: int = 0
    # These were previously sent by the setup page but dropped here (BaseModel
    # ignores unknown fields), so idle timeouts, key overrides, and the Stream
    # Deck weather never persisted through /save. Declared so they round-trip.
    streamdeck_idle_timeout: int = 0
    display_idle_timeout: int = 0
    streamdeck_key_overrides: list = []
    streamdeck_weather_location: str = ""
    streamdeck_weather_units: str = "f"
    streamdeck_key_style: str = ""
    streamdeck_icon_color: str = ""
    streamdeck_cameras: list = []
    streamdeck_ha_base_url: str = ""
    streamdeck_ha_token: str = ""
    streamdeck_ha_slots: list = []
    ha_events_enabled: bool = False
    ha_camera_popup_seconds: int = 20
    convert_custom_rows: list = []
    floating_nav_position: str = ""
    floating_nav_orientation: str = ""
    floating_nav_autohide_streamdeck: bool = False
    display_touch: bool = False
    auth_required: bool = True
    auth_password: str = ""
    api_key: str = ""
    # Each row is {"key": <secret or __KEEP__:i>, "name": <label>}; a bare
    # string is still accepted (see _merge_satellite_keys).
    extra_api_keys: list[dict | str] | None = None
    rclone_remote: str = ""
    rclone_schedule_hours: int = 0


class TestGrocyPayload(BaseModel):
    grocy_base_url: str = ""
    grocy_api_key: str = ""


class TestMealiePayload(BaseModel):
    mealie_base_url: str = ""
    mealie_api_key: str = ""


class TestRemotePayload(BaseModel):
    remote_server_url: str = ""


@router.post("/test/remote")
async def test_remote(payload: TestRemotePayload):
    """Check that a Pi Remote can reach the FoodAssistant server it controls."""
    url = (payload.remote_server_url or settings.remote_server_url).rstrip("/")
    if not url:
        return {"ok": False, "error": "Server URL is required."}
    try:
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
            r = await client.get(f"{url}/health")
        if r.status_code == 200:
            return {"ok": True, "message": f"Connected: FoodAssistant reachable at {url}"}
        return {"ok": False, "error": f"HTTP {r.status_code} from {url}/health"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class SatelliteSyncPayload(BaseModel):
    remote_server_url: str = ""
    upstream_api_key: str = ""


@router.post("/satellite/sync")
async def satellite_sync(payload: SatelliteSyncPayload):
    """Save the upstream link, then pull backend config + defaults from it.

    Used by the satellite setup flow: enter the main server URL + API key, then
    sync. On success the satellite has Grocy/Mealie/AI config and is usable.
    """
    data = {"deployment_mode": "pi_remote"}
    if payload.remote_server_url:
        data["remote_server_url"] = payload.remote_server_url.rstrip("/")
    if payload.upstream_api_key and payload.upstream_api_key != _CLEAR:
        data["upstream_api_key"] = payload.upstream_api_key
    settings.save(data)

    from ..services.satellite import sync_from_upstream
    result = await run_in_threadpool(sync_from_upstream)
    # The recorded last-sync summary (timestamp, ok flag, applied fields) lets
    # the page redraw its Sync Status panel without a full reload.
    last = settings.satellite_last_sync if isinstance(settings.satellite_last_sync, dict) else {}
    if result.get("ok"):
        return {
            "ok": True,
            "message": f"Synced {len(result['applied'])} settings and "
                       f"{result['defaults']} expiry defaults from the server.",
            "last_sync": last,
        }
    return {
        "ok": False,
        "error": result.get("error", "Sync failed."),
        "last_sync": last,
    }


class TestProviderPayload(BaseModel):
    provider: str
    api_key: str = ""
    model: str = ""
    base_url: str = ""   # ollama only


class TestRecipesPayload(BaseModel):
    source: str = "themealdb"
    api_key: str = ""


_LOCAL_GROCY_CANDIDATES = [
    "http://localhost:9383",
    "http://127.0.0.1:9383",
    "http://grocy:80",
]


async def _detect_local_grocy() -> str:
    """Return the first candidate Grocy URL that responds with a 200/401, or ''."""
    async with httpx.AsyncClient(timeout=1.5) as client:
        for url in _LOCAL_GROCY_CANDIDATES:
            try:
                r = await client.get(f"{url}/api/system/info")
                if r.status_code in (200, 401):
                    return url
            except Exception:
                pass
    return ""


def _pi_mdns_host() -> str:
    """Return the device's own browser host, e.g. '<hostname>.local'.

    Uses the resolved device hostname (a user override, the real host hostname
    from the host bridge, or socket.gethostname()), not a hardcoded name, so it
    works when several appliances share a LAN. Falls back to the LAN IP if no
    hostname is resolvable.
    """
    return browser_host()


def _grocy_url_for_api(request: Request, detected: str) -> str:
    """The Grocy URL to pre-fill as the server-side API base.

    This is the address the app process (in a container on an appliance) uses to
    call Grocy, so it must be reachable from there. On a Pi we keep the detected
    loopback/Docker address rather than a <hostname>.local link, because mDNS may
    not resolve inside the container. Off a Pi, when reached from another machine
    we substitute the request hostname so the pre-filled value works there too.
    The human "open in browser" link is computed separately (see grocy_link_url).
    """
    if not detected:
        return detected
    if is_raspberry_pi():
        return detected
    client_host = (request.client.host if request.client else "") or ""
    if client_host in ("127.0.0.1", "::1", "localhost", ""):
        return detected
    server_host = request.url.hostname or client_host
    return f"http://{server_host}:9383"


def _suggest_mealie_url(request: Request) -> str:
    """Return a suggested Mealie URL for the browser, or '' if not applicable.

    On a Pi appliance Mealie runs (or will run) on the same host at port 9285.
    Prefers the mDNS hostname so the link survives DHCP IP changes.
    Returns '' when Mealie is already configured or we are not on a Pi.
    """
    if not is_raspberry_pi():
        return ""
    if settings.mealie_base_url:
        return ""
    host = _pi_mdns_host()
    return f"http://{host}:9285" if host else ""


def available_modes() -> dict:
    """Deployment modes offered on this host.

    On a Raspberry Pi we offer the two Pi modes and hide "Server hosted"
    (which targets a general server). Elsewhere only "Server hosted" applies.
    """
    pi = is_raspberry_pi()
    return {k: v for k, v in DEPLOYMENT_MODES.items() if v["pi"] == pi}


@router.get("", response_class=HTMLResponse)
async def setup_page(request: Request):
    suggested_grocy_url = ""
    if not settings.grocy_base_url or settings.grocy_base_url == "http://grocy:80":
        raw = await _detect_local_grocy()
        suggested_grocy_url = _grocy_url_for_api(request, raw)
    # Human-facing link to open Grocy in a browser. Prefers the configured
    # browser URL (public URL, else the base URL rewritten to <hostname>.local).
    grocy_browser_link = settings.grocy_link_url()
    modes = available_modes()
    # Default the picker: keep the saved choice if still valid, else the only
    # mode that fits this host (or the generic default).
    current_mode = settings.deployment_mode
    if current_mode not in modes:
        current_mode = next(iter(modes), _DEFAULT_DEPLOYMENT_MODE)
    return templates.TemplateResponse(request, "setup.html", {
        "request": request,
        "s": settings,
        "configured": settings.is_configured(),
        # booleans only: never the stored secrets themselves
        "has": {f: bool(getattr(settings, f, "")) for f in _SECRET_FIELDS},
        # count of stored extra keys per provider (values never sent to the page)
        "extra_key_counts": {
            p: len([k for k in (settings.ai_extra_keys.get(p, []) if isinstance(settings.ai_extra_keys, dict) else []) if k])
            for p in ("gemini", "openai", "anthropic")
        },
        # count of stored satellite extra keys (values never sent to the page)
        "extra_api_key_count": len([k for k in (settings.extra_api_keys if isinstance(settings.extra_api_keys, list) else []) if k]),
        "extra_api_key_names": (settings.extra_api_key_names if isinstance(settings.extra_api_key_names, list) else []),
        "ai_models": AI_MODELS,
        "tabs": all_tabs(),
        "version": APP_VERSION,
        "custom_categories": custom_categories(),
        "themes": THEMES,
        "ui_scales": UI_SCALES,
        "display_rotations": DISPLAY_ROTATIONS,
        "display_types": DISPLAY_TYPES,
        "suggested_grocy_url": suggested_grocy_url,
        "grocy_browser_link": grocy_browser_link,
        "suggested_mealie_url": _suggest_mealie_url(request),
        "deployment_modes": modes,
        "current_mode": current_mode,
        "is_pi": is_raspberry_pi(),
        "is_satellite": settings.is_satellite(),
        "board_model": board_model(),
        "pi_mdns_host": _pi_mdns_host() if is_raspberry_pi() else "",
        # Kitchen-appliance checklist, grouped for the Preferences section, with
        # each item's current checked state from the saved selection.
        "appliance_groups": _appliance_groups(),
    })


def _appliance_groups() -> dict:
    """Group the appliance catalog into major/minor with each item's checked
    state, so the Preferences checklist renders without logic in the template."""
    selected = set(settings.kitchen_appliances or [])
    groups: dict[str, list] = {"major": [], "minor": []}
    for key, label, group, _default in KITCHEN_APPLIANCES:
        groups.get(group, groups["minor"]).append(
            {"key": key, "label": label, "checked": key in selected}
        )
    return groups


class ModePayload(BaseModel):
    deployment_mode: str = _DEFAULT_DEPLOYMENT_MODE
    remote_server_url: str = ""


@router.post("/mode")
async def save_mode(payload: ModePayload):
    """Persist the deployment mode chosen on wizard step 1.

    Saved on its own (before the rest of setup) so the wizard can branch and,
    on a Pi, the provisioner can read the choice to decide what to install.
    """
    mode = payload.deployment_mode
    if mode not in DEPLOYMENT_MODES:
        return JSONResponse({"ok": False, "error": "Unknown deployment mode."})
    data = {"deployment_mode": mode}
    if mode == "pi_remote":
        data["remote_server_url"] = payload.remote_server_url.rstrip("/")
    settings.save(data)
    return {"ok": True, "mode": mode}


class ThemePayload(BaseModel):
    ui_theme: str = _DEFAULT_THEME


@router.post("/theme")
async def save_theme(payload: ThemePayload):
    settings.save({"ui_theme": payload.ui_theme})
    # Recolour an attached Stream Deck to match the new theme (gxl). Best-effort
    # and Pi-only: pushes the theme into the controller config.toml via the
    # bridge so the running deck updates without a manual Stream Deck save.
    if is_raspberry_pi() and settings.has_streamdeck:
        from ..services.satellite import _push_streamdeck_settings
        _push_streamdeck_settings()
    return {"ok": True}


class ScalePayload(BaseModel):
    ui_scale: str = _DEFAULT_UI_SCALE
    display_rotation: int = _DEFAULT_DISPLAY_ROTATION


@router.post("/scale")
async def save_scale(payload: ScalePayload):
    scale = payload.ui_scale if payload.ui_scale in UI_SCALES else _DEFAULT_UI_SCALE
    rot = payload.display_rotation if payload.display_rotation in DISPLAY_ROTATIONS else _DEFAULT_DISPLAY_ROTATION
    settings.save({"ui_scale": scale, "display_rotation": rot})
    return {"ok": True}


@router.post("/save")
async def save_setup(payload: SetupPayload):
    data = payload.model_dump(exclude_unset=True)
    # On a satellite the backend config is owned by the main server and pulled on
    # each sync. Drop any user edit to those fields here so it is not saved and
    # then silently overwritten on the next sync (the panes show them read-only,
    # but this guards a stray or scripted POST). The sync path persists pulled
    # values through settings.save() directly, which this does not touch.
    if settings.is_satellite():
        for f in SATELLITE_PULL_FIELDS:
            data.pop(f, None)
    for f in _SECRET_FIELDS:
        if data.get(f) == "":
            data.pop(f, None)        # blank = keep existing value
        elif data.get(f) == _CLEAR:
            data[f] = ""             # explicit clear
    data["ai_extra_keys"] = _merge_extra_keys(data.get("ai_extra_keys"))
    if data["ai_extra_keys"] is None:
        data.pop("ai_extra_keys", None)   # absent = keep stored extras
    merged_sat = _merge_satellite_keys(data.get("extra_api_keys"))
    if merged_sat is None:
        data.pop("extra_api_keys", None)  # absent = keep stored extras
        data.pop("extra_api_key_names", None)
    else:
        data["extra_api_keys"], data["extra_api_key_names"] = merged_sat
    if data.get("display_rotation") not in DISPLAY_ROTATIONS:
        data["display_rotation"] = _DEFAULT_DISPLAY_ROTATION
    # Drop an unknown display type rather than persisting a broken value; an
    # absent value leaves the stored choice untouched.
    if "display_type" in data and data["display_type"] not in DISPLAY_TYPES:
        data.pop("display_type", None)
    # Drop an unknown floating-nav position (empty/invalid keeps the stored one).
    if "floating_nav_position" in data and data["floating_nav_position"] not in FLOATING_NAV_POSITIONS:
        data.pop("floating_nav_position", None)
    # Same for an unknown floating-nav orientation.
    if "floating_nav_orientation" in data and data["floating_nav_orientation"] not in FLOATING_NAV_ORIENTATIONS:
        data.pop("floating_nav_orientation", None)
    # Drop an unknown Stream Deck key style / icon colour (keeps the stored one).
    if "streamdeck_key_style" in data and data["streamdeck_key_style"] not in STREAMDECK_KEY_STYLES:
        data.pop("streamdeck_key_style", None)
    if "streamdeck_icon_color" in data and data["streamdeck_icon_color"] not in STREAMDECK_ICON_COLORS:
        data.pop("streamdeck_icon_color", None)
    # Keep only known appliance ids, de-duplicated in catalog order, so a stale
    # or hand-crafted id never reaches the AI prompt. An empty list is preserved
    # (the user owns none); the field absent leaves the stored choice untouched.
    if "kitchen_appliances" in data:
        submitted = data["kitchen_appliances"]
        if isinstance(submitted, list):
            chosen = {str(x) for x in submitted}
            data["kitchen_appliances"] = [k for k in KITCHEN_APPLIANCE_KEYS if k in chosen]
        else:
            data.pop("kitchen_appliances", None)
    # Drop an unknown deployment mode rather than persisting a broken value;
    # an empty/absent mode leaves the existing choice untouched.
    if data.get("deployment_mode") and data["deployment_mode"] not in DEPLOYMENT_MODES:
        data.pop("deployment_mode", None)
    if data.get("remote_server_url"):
        data["remote_server_url"] = data["remote_server_url"].rstrip("/")
    settings.save(data)
    reset_providers()   # apply new provider/model/key without a restart
    from ..services.mealie import reset_cache as reset_mealie_cache, reset_staple_cache
    reset_mealie_cache()
    reset_staple_cache()
    # Mirror the kiosk display idle timeout to the host bridge, which owns the
    # blanking loop (FoodAssistant-otiy). Best-effort and Pi-only.
    if "display_idle_timeout" in data:
        await _push_display_idle()
    return {"ok": True}


class StorageCategoriesPayload(BaseModel):
    categories: list[dict] = []


@router.post("/storage-categories")
async def save_storage_categories(payload: StorageCategoriesPayload):
    """Replace the set of user-defined storage categories.

    Entries are normalized/validated (blank, keyless, or built-in-colliding
    rows are dropped) before saving, so the inventory dashboard never sees a
    malformed category.
    """
    clean = [storable(c) for c in _normalize_custom(payload.categories)]
    settings.save({"custom_storage_categories": clean})
    return {"ok": True, "categories": clean}


def _is_local_grocy_host(url: str) -> bool:
    """True when url names this device (loopback, the Docker service, or the
    device's own <hostname>.local / LAN address). Such an address may be entered
    as a browser link the app container cannot itself reach, so the test is
    allowed to fall back to loopback candidates. A genuinely remote Grocy is
    tested only at the URL given, so we never mask its auth error with a
    different, co-hosted Grocy."""
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0", "grocy"}:
        return True
    own = (device_hostname() or "").lower()
    if own and host in (own, f"{own}.local"):
        return True
    return False


def _grocy_test_targets(entered: str) -> list[str]:
    """Server-side URLs to try for a Grocy connection test, best first.

    The user may enter a browser-facing address for a co-hosted Grocy (e.g.
    http://<host>.local:9383 or http://127.0.0.1:9383) that the app container
    cannot resolve or reach, even though Grocy is up. For such local addresses we
    try the entered URL first, then fall back to the addresses the app process
    can actually use (the Docker service name and loopback on the published
    port). A remote Grocy URL is tested as-is, with no fallback. Duplicates are
    dropped while preserving order.
    """
    entered = (entered or "").rstrip("/")
    candidates = [entered]
    if _is_local_grocy_host(entered):
        candidates += _LOCAL_GROCY_CANDIDATES
    targets: list[str] = []
    for u in candidates:
        u = (u or "").rstrip("/")
        if u and u not in targets:
            targets.append(u)
    return targets


@router.post("/test/grocy")
async def test_grocy(payload: TestGrocyPayload):
    entered = (payload.grocy_base_url or settings.grocy_base_url).rstrip("/")
    key = payload.grocy_api_key or settings.grocy_api_key
    if not entered or not key:
        return JSONResponse({"ok": False, "error": "URL and API key are both required."})

    # Grocy's API needs the GROCY-API-KEY header; a bare browser hit returns 401.
    # We always test the API endpoint with the key so "reachable" means "usable".
    last_unreachable = ""
    auth_failure = ""
    for url in _grocy_test_targets(entered):
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                r = await client.get(f"{url}/api/system/info",
                                     headers={"GROCY-API-KEY": key})
        except Exception as e:
            last_unreachable = _safe_error(e, key)
            continue
        if r.status_code == 200:
            version = r.json().get("grocy_version", "?")
            note = "" if url == entered else f" (reached via {url})"
            return {"ok": True, "message": f"Connected: Grocy {version}{note}"}
        if r.status_code in (401, 403):
            # Reached Grocy, but it rejected the key. Distinct from unreachable:
            # the address is good, the API key is wrong or lacks permissions.
            # Keep the first reachable URL's report (the one the user entered).
            if not auth_failure:
                auth_failure = (f"Grocy is reachable at {url} but rejected the API "
                                f"key (HTTP {r.status_code}). Check the key under "
                                f"Grocy, Profile, Manage API keys.")
            continue
        last_unreachable = f"HTTP {r.status_code}: {_safe_error(r.text[:200], key)}"

    if auth_failure:
        return {"ok": False, "error": auth_failure}
    return {"ok": False,
            "error": last_unreachable or f"Could not reach Grocy at {entered}."}


@router.post("/test/mealie")
async def test_mealie(payload: TestMealiePayload):
    url = (payload.mealie_base_url or settings.mealie_base_url).rstrip("/")
    key = payload.mealie_api_key or settings.mealie_api_key
    if not url or not key:
        return JSONResponse({"ok": False, "error": "URL and API token are both required."})
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(f"{url}/api/users/self",
                                 headers={"Authorization": f"Bearer {key}"})
        if r.status_code == 200:
            user = r.json().get("username") or r.json().get("email", "?")
            return {"ok": True, "message": f"Connected: authenticated as {user}"}
        return {"ok": False, "error": f"HTTP {r.status_code}: {_safe_error(r.text[:200], key)}"}
    except Exception as e:
        return {"ok": False, "error": _safe_error(e, key)}


@router.post("/test/provider")
async def test_provider(payload: TestProviderPayload):
    """Connection test for any LLM provider (Vision and Enrichment sections)."""
    p = payload.provider
    saved_key = getattr(settings, f"{p}_api_key", "")
    key = payload.api_key or saved_key

    if p == "gemini":
        if not key:
            return {"ok": False, "error": "Gemini API key is required."}
        try:
            import google.generativeai as genai
            genai.configure(api_key=key)
            model = payload.model or "gemini-2.5-flash"
            genai.get_model(f"models/{model}")
            return {"ok": True, "message": f"Connected: model {model} available."}
        except Exception as e:
            return {"ok": False, "error": _safe_error(e, key)}

    if p == "ollama":
        url = (payload.base_url or settings.ollama_base_url or "http://localhost:11434").rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                r = await client.get(f"{url}/api/tags")
            if r.status_code == 200:
                models = [m["name"] for m in r.json().get("models", [])]
                model_list = ", ".join(models) if models else "none installed"
                return {"ok": True, "message": f"Connected: models: {model_list}"}
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    if p == "openai":
        if not key:
            return {"ok": False, "error": "OpenAI API key is required."}
        model = payload.model or "gpt-4o-mini"
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(f"https://api.openai.com/v1/models/{model}",
                                     headers={"Authorization": f"Bearer {key}"})
            if r.status_code == 200:
                return {"ok": True, "message": f"Connected: model {model} available."}
            return {"ok": False, "error": f"HTTP {r.status_code}: {_safe_error(r.text[:200], key)}"}
        except Exception as e:
            return {"ok": False, "error": _safe_error(e, key)}

    if p == "anthropic":
        if not key:
            return {"ok": False, "error": "Anthropic API key is required."}
        model = payload.model or "claude-opus-4-8"
        try:
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=key)
            await client.models.retrieve(model)
            return {"ok": True, "message": f"Connected: model {model} available."}
        except Exception as e:
            return {"ok": False, "error": _safe_error(e, key)}

    return {"ok": False, "error": "Unknown provider."}


@router.post("/test/recipes")
async def test_recipes(payload: TestRecipesPayload):
    """Connection test for the external recipe source."""
    if payload.source == "spoonacular":
        key = payload.api_key or settings.spoonacular_api_key
        if not key:
            return {"ok": False, "error": "Spoonacular API key is required."}
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(
                    "https://api.spoonacular.com/recipes/findByIngredients",
                    params={"ingredients": "apple", "number": 1, "apiKey": key})
            if r.status_code == 200:
                quota = r.headers.get("x-api-quota-left", "?")
                return {"ok": True, "message": f"Connected: quota left today: {quota} points."}
            return {"ok": False, "error": f"HTTP {r.status_code}: {_safe_error(r.text[:200], key)}"}
        except Exception as e:
            return {"ok": False, "error": _safe_error(e, key)}

    if payload.source == "themealdb":
        key = payload.api_key or settings.themealdb_api_key or "1"
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(
                    f"https://www.themealdb.com/api/json/v1/{key}/filter.php",
                    params={"i": "chicken"})
            if r.status_code == 200 and (r.json() or {}).get("meals"):
                kind = "public key" if key == "1" else "premium key"
                return {"ok": True, "message": f"Connected: TheMealDB reachable ({kind})."}
            return {"ok": False, "error": f"HTTP {r.status_code}: {_safe_error(r.text[:200], key)}"}
        except Exception as e:
            return {"ok": False, "error": _safe_error(e, key)}

    if payload.source == "off":
        return {"ok": True, "message": "External suggestions disabled."}
    return {"ok": False, "error": "Unknown source."}


# TOTP 2FA setup endpoints

class TOTPVerifyPayload(BaseModel):
    secret: str
    code: str


@router.post("/totp/generate")
async def totp_generate():
    """Generate a new TOTP secret and return the provisioning URI for QR display."""
    import pyotp, qrcode, base64, io
    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name="FoodAssistant", issuer_name="FoodAssistant")
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return {"secret": secret, "qr": f"data:image/png;base64,{b64}", "uri": uri}


@router.post("/totp/verify")
async def totp_verify(payload: TOTPVerifyPayload):
    """Confirm the user's authenticator app is in sync before enabling TOTP."""
    import pyotp
    try:
        totp = pyotp.TOTP(payload.secret)
        ok = totp.verify(payload.code.strip(), valid_window=1)
    except Exception:
        ok = False
    if ok:
        settings.save({"totp_secret": payload.secret})
        return {"ok": True, "message": "Two-factor authentication enabled."}
    return {"ok": False, "error": "Code did not match. Check your authenticator app clock."}


@router.post("/totp/disable")
async def totp_disable():
    """Remove the stored TOTP secret, disabling 2FA."""
    settings.save({"totp_secret": ""})
    return {"ok": True, "message": "Two-factor authentication disabled."}


# Pi host bridge endpoints
# -------------------------
# These call a small helper service running on 127.0.0.1:9299 on the Pi host.
# Because docker-compose.appliance.yml uses network_mode: host, localhost in the
# container is the same as localhost on the host, so no special networking is needed.
# On non-Pi or non-appliance installs the endpoints return a clear error.

_HOST_BRIDGE = "http://127.0.0.1:9299"


async def _push_display_idle() -> bool:
    """Push the display idle timeout to the host bridge (Pi only, best-effort).

    The bridge owns the kiosk display blanking loop and persists this value, so
    the browser does not need its own timer (FoodAssistant-otiy)."""
    if not is_raspberry_pi():
        return False
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            r = await c.post(
                f"{_HOST_BRIDGE}/display/idle",
                json={"minutes": settings.display_idle_timeout},
            )
        return r.status_code == 200
    except Exception:
        return False


@router.post("/kiosk/activity")
async def kiosk_activity():
    """Report kiosk user activity to the host bridge so it wakes the display
    (and the Stream Deck, which polls the bridge). No-op off a Pi."""
    if not is_raspberry_pi():
        return {"ok": True, "woke": False}
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/activity", json={"source": "kiosk"})
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/kiosk/activity")
async def kiosk_activity_state():
    """Current shared activity/display state from the host bridge."""
    if not is_raspberry_pi():
        return {"ok": True, "last_activity": 0, "display_blanked": False}
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            r = await c.get(f"{_HOST_BRIDGE}/activity")
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/display/blank")
async def display_blank():
    """Manually blank the kiosk display (Pi only)."""
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    try:
        async with httpx.AsyncClient(timeout=6.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/display/blank")
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/display/wake")
async def display_wake():
    """Manually wake the kiosk display (Pi only)."""
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    try:
        async with httpx.AsyncClient(timeout=6.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/display/wake")
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/network/status")
async def network_status():
    """Current Wi-Fi SSID and hostname, via the Pi host bridge."""
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            wifi = (await c.get(f"{_HOST_BRIDGE}/wifi/status")).json()
            hn = (await c.get(f"{_HOST_BRIDGE}/hostname")).json()
        return {
            "ok": True,
            "ssid": wifi.get("ssid", ""),
            "wifi_state": wifi.get("state", ""),
            "wifi_detail": wifi.get("detail", ""),
            "ethernet": wifi.get("ethernet", {}),
            "hostname": hn.get("hostname", ""),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/network/scan")
async def network_scan():
    """List visible Wi-Fi networks, via the Pi host bridge."""
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    try:
        async with httpx.AsyncClient(timeout=25.0) as c:
            r = (await c.get(f"{_HOST_BRIDGE}/wifi/scan")).json()
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)}


class WifiPayload(BaseModel):
    ssid: str = ""
    password: str = ""


@router.post("/network/wifi")
async def network_wifi(payload: WifiPayload):
    """Connect to a Wi-Fi network (Pi appliance only)."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    if not payload.ssid.strip():
        return JSONResponse({"ok": False, "error": "SSID is required."})
    try:
        async with httpx.AsyncClient(timeout=35.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/wifi/connect",
                             json={"ssid": payload.ssid.strip(), "password": payload.password})
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


class HostnamePayload(BaseModel):
    hostname: str = ""


@router.post("/network/hostname")
async def network_hostname(payload: HostnamePayload):
    """Change the device hostname (Pi appliance only)."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    name = payload.hostname.strip().lower()
    if not name:
        return JSONResponse({"ok": False, "error": "Hostname is required."})
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/hostname", json={"hostname": name})
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.get("/display/rotation")
async def display_rotation_status():
    """Current KMS framebuffer rotation (Pi appliance only)."""
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            r = (await c.get(f"{_HOST_BRIDGE}/display/rotation")).json()
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)}


class KmsRotationPayload(BaseModel):
    degrees: int = 0
    reboot: bool = False


@router.post("/display/rotation")
async def set_display_rotation(payload: KmsRotationPayload):
    """Set the KMS framebuffer rotation (Pi appliance only). Takes effect after reboot."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    if payload.degrees not in (0, 90, 180, 270):
        return JSONResponse({"ok": False, "error": "degrees must be 0, 90, 180, or 270."})
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/display/rotation",
                             json={"degrees": payload.degrees, "reboot": payload.reboot})
        settings.save({"display_rotation": payload.degrees})
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/streamdeck/restart")
async def streamdeck_restart():
    """Restart the Stream Deck systemd service (Pi appliance only)."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/streamdeck/restart")
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/kiosk/restart")
async def kiosk_restart():
    """Restart the kiosk browser so display scale/rotation changes apply."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    try:
        async with httpx.AsyncClient(timeout=35.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/kiosk/restart")
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/mealie/start")
async def mealie_start():
    """Kick off the Mealie container start on a Pi appliance via the host bridge.

    The bridge runs the image pull/up in the background and returns at once, so
    a short timeout is enough; the web UI polls /mealie/status for progress.
    """
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/mealie/start")
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.get("/mealie/status")
async def mealie_status():
    """Mealie start progress (not-installed / starting / running), via the bridge."""
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    try:
        async with httpx.AsyncClient(timeout=12.0) as c:
            r = (await c.get(f"{_HOST_BRIDGE}/mealie/status")).json()
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)}


_LOG_NAMES = {"mealie", "kiosk", "streamdeck"}


@router.get("/logs/{name}")
async def install_logs(name: str):
    """Tail of an install/start log (mealie / kiosk / streamdeck), via the bridge.

    Mirrors the /mealie/status proxy: the setup UI polls this while a start or
    install is in flight to show live output. Returns {ok, name, running,
    lines}. Unknown names and bridge errors are reported, never raised.
    """
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    if name not in _LOG_NAMES:
        return {"ok": False, "error": "unknown log name"}
    try:
        async with httpx.AsyncClient(timeout=6.0) as c:
            r = (await c.get(f"{_HOST_BRIDGE}/logs/{name}")).json()
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/hardware/status")
async def hardware_status():
    """Display / Stream Deck presence and service state, via the Pi host bridge.

    Off a Pi there is no host bridge to probe, so return a clean "nothing
    attached" shape (rather than an error) so the setup UI's attached-hardware
    panel degrades gracefully instead of showing a failure.
    """
    if not is_raspberry_pi():
        return {
            "ok": True,
            "display": {"present": False, "connectors": []},
            "streamdeck": {"present": False, "model": ""},
        }
    try:
        async with httpx.AsyncClient(timeout=6.0) as c:
            r = (await c.get(f"{_HOST_BRIDGE}/hardware/status")).json()
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/system/health")
async def system_health():
    """Pi power/thermal/disk warnings, via the host bridge.

    Off a Pi there is no bridge to probe, so return a clean "no warnings" shape
    (rather than an error) so the navbar indicator simply shows nothing instead
    of a failure on a server or phone.
    """
    if not is_raspberry_pi():
        return {"ok": True, "warnings": []}
    try:
        async with httpx.AsyncClient(timeout=6.0) as c:
            r = (await c.get(f"{_HOST_BRIDGE}/system/health")).json()
        return r
    except Exception as e:
        return {"ok": False, "error": str(e), "warnings": []}


@router.post("/kiosk/install")
async def kiosk_install():
    """Provision the kiosk service for a display attached after first install."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/kiosk/install")
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/update")
async def update_software():
    """Pull the latest source and restart the service, via the host bridge.

    Only meaningful on a Pi Remote (satellite): that device runs the app from a
    Python venv with no Docker image to re-pull, so the host bridge shells out
    to foodassistant-update (git pull, venv pip when requirements changed, then
    a service restart). The bridge runs the work synchronously and returns
    {ok, before, after, restarted, log}; a failed pull leaves the running
    version untouched and reports the error. The pull plus a pip install can
    take a couple of minutes on a Pi, so the proxy timeout is generous.
    """
    if not settings.is_satellite():
        return JSONResponse(
            {"ok": False, "error": "Updates are only available on Pi Remote devices."})
    try:
        async with httpx.AsyncClient(timeout=620.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/update")
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


class _RestoreReq(BaseModel):
    source: str = ""


@router.post("/restore")
async def restore_full_stack(req: _RestoreReq):
    """Full Grocy + Mealie + app snapshot restore, via the host bridge.

    Only meaningful on a Pi appliance: the restore stops and restarts the whole
    docker stack and swaps the bind-mounted data dirs, which only the host
    bridge (running as root) can do. We do NOT accept a browser upload of the
    (large) snapshot; instead the body's {source} is either an absolute path to
    a .tar.gz already on the device, or "rclone:<remote-path>" to pull from the
    configured rclone remote first. The bridge runs foodassistant-restore
    synchronously and returns {ok, error, source, restored_dirs, snapshot,
    restarted, log}; the helper sets the current data aside (.pre-restore-<stamp>,
    never deleted) and tries to restart on any mid-restore failure so the device
    is never left down. Stopping, pulling, unpacking and restarting can take a
    while on a Pi, so the proxy timeout is generous.
    """
    if not is_raspberry_pi():
        return JSONResponse(
            {"ok": False, "error": "Full-stack restore is only available on this appliance."})
    try:
        async with httpx.AsyncClient(timeout=920.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/restore", json={"source": req.source})
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.get("/streamdeck/config")
async def streamdeck_config_get():
    """Proxy GET config from host bridge.

    On a satellite the Stream Deck weather is owned by the main server, so the
    returned config's weather fields are overlaid with the synced settings. That
    keeps the setup UI showing the server's location/units (rendered read-only)
    even if the local config.toml still holds older values.
    """
    if _HOST_BRIDGE:
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                r = await c.get(f"{_HOST_BRIDGE}/streamdeck/config")
                content = r.json()
                if r.status_code == 200 and settings.is_satellite() and isinstance(content.get("config"), dict):
                    content["config"]["weather_location"] = settings.streamdeck_weather_location
                    content["config"]["weather_units"] = settings.streamdeck_weather_units
                return JSONResponse(status_code=r.status_code, content=content)
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})
    return JSONResponse(status_code=500, content={"ok": False, "error": "bridge unavailable"})


@router.post("/streamdeck/config")
async def streamdeck_config_set(request: Request):
    """Proxy POST config to host bridge.

    On a satellite the weather config comes from the main server, so any weather
    values in the posted config are replaced with the synced settings before the
    write. This stops a local save from diverging the deck from the server.
    """
    if _HOST_BRIDGE:
        try:
            payload = await request.json()
            if isinstance(payload.get("config"), dict):
                # Stamp the active web UI theme so the deck follows it (gxl), plus
                # the key style + icon colour so the deck matches the app setting.
                payload["config"]["theme"] = settings.ui_theme
                payload["config"]["key_style"] = settings.streamdeck_key_style
                payload["config"]["icon_color"] = settings.streamdeck_icon_color
                # Home Assistant credentials, key map, and cameras live in app
                # settings (one source of truth, server or Pi). Stamp them so the
                # deck always gets the server's values regardless of what the page
                # posted, and a satellite's deck inherits them (FoodAssistant-cr50).
                payload["config"]["ha_base_url"] = settings.streamdeck_ha_base_url
                payload["config"]["ha_token"] = settings.streamdeck_ha_token
                payload["config"]["ha_slots"] = settings.streamdeck_ha_slots
                payload["config"]["cameras"] = [
                    {"name": c.get("name", ""), "snapshot_url": c.get("snapshot_url", ""),
                     "ha_entity": c.get("ha_entity", "")}
                    for c in settings.streamdeck_cameras if isinstance(c, dict)
                ]
                if settings.is_satellite():
                    payload["config"]["weather_location"] = settings.streamdeck_weather_location
                    payload["config"]["weather_units"] = settings.streamdeck_weather_units
            async with httpx.AsyncClient(timeout=3.0) as c:
                r = await c.post(f"{_HOST_BRIDGE}/streamdeck/config", json=payload)
                return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})
    return JSONResponse(status_code=500, content={"ok": False, "error": "bridge unavailable"})


def _ha_camera_urls(base: str, token: str, entity: str) -> dict:
    """Build the HLS stream and still-snapshot URLs for an HA camera entity.

    The token is embedded in the URL (the camera proxy endpoints accept a
    ``?token=`` query param), so the kiosk <img>/<video> and the deck snapshot
    can fetch the feed without an auth header. Built server-side so the token
    never has to leave the server to be turned into a usable URL.
    """
    from urllib.parse import quote
    b = base.rstrip("/")
    e = quote(entity, safe="")
    t = quote(token, safe="")
    return {
        "stream_url": f"{b}/api/camera_proxy_stream/{e}?token={t}",
        "snapshot_url": f"{b}/api/camera_proxy/{e}?token={t}",
    }


class HaCameraDiscoverPayload(BaseModel):
    # Optional overrides so a freshly typed (not yet saved) base/token can be
    # tested before the user commits them. Blank falls back to saved settings.
    base_url: str = ""
    token: str = ""


@router.post("/ha/cameras")
async def ha_discover_cameras(payload: HaCameraDiscoverPayload):
    """Discover Home Assistant camera entities and build their feed URLs.

    Queries HA ``/api/states`` with the long-lived access token, keeps the
    ``camera.*`` entities, and returns each with a friendly name plus prebuilt
    stream and snapshot URLs. Credentials come from app settings (one source of
    truth, pulled by satellites), but the request body may override them so the
    Cameras page can test a token before it is saved. The token stays on the
    server: only the finished URLs (which embed it) come back.
    """
    base = (payload.base_url or settings.streamdeck_ha_base_url or "").strip().rstrip("/")
    token = (payload.token or settings.streamdeck_ha_token or "").strip()
    if not base or not token:
        return {"ok": False, "error": "Set the Home Assistant URL and token first."}
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(
                f"{base}/api/states",
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception as e:
        return {"ok": False, "error": f"Could not reach Home Assistant: {e}"}
    if r.status_code in (401, 403):
        return {"ok": False, "error": "Home Assistant rejected the token (401/403)."}
    if r.status_code != 200:
        return {"ok": False, "error": f"Home Assistant returned HTTP {r.status_code}."}
    try:
        states = r.json()
    except Exception:
        return {"ok": False, "error": "Unexpected response from Home Assistant."}
    cameras = []
    for st in states if isinstance(states, list) else []:
        entity = st.get("entity_id", "") if isinstance(st, dict) else ""
        if not entity.startswith("camera."):
            continue
        attrs = st.get("attributes") or {}
        name = attrs.get("friendly_name") or entity.split(".", 1)[1].replace("_", " ").title()
        cameras.append({"entity_id": entity, "name": name, **_ha_camera_urls(base, token, entity)})
    cameras.sort(key=lambda c: c["name"].lower())
    return {"ok": True, "cameras": cameras}


@router.get("/streamdeck/actions")
async def streamdeck_actions():
    """List assignable Stream Deck actions for the grid editor (Pi only)."""
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    try:
        async with httpx.AsyncClient(timeout=12.0) as c:
            r = (await c.get(f"{_HOST_BRIDGE}/streamdeck/actions")).json()
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/streamdeck/install")
async def streamdeck_install():
    """Provision the Stream Deck service for a deck attached after first install."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/streamdeck/install")
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


class ProfileSavePayload(BaseModel):
    name: str
    deck_size: int
    key_overrides: list = []


def _profile_to_dict(p: StreamDeckProfile) -> dict:
    try:
        overrides = json.loads(p.key_overrides or "[]")
    except Exception:
        overrides = []
    return {
        "name": p.name,
        "deck_size": p.deck_size,
        "key_overrides": overrides,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


@router.get("/streamdeck/profiles")
async def streamdeck_profiles_list():
    """List saved Stream Deck profiles."""
    db = SessionLocal()
    try:
        rows = db.query(StreamDeckProfile).order_by(StreamDeckProfile.name).all()
        return {"ok": True, "profiles": [_profile_to_dict(r) for r in rows]}
    finally:
        db.close()


@router.post("/streamdeck/profiles")
async def streamdeck_profiles_save(payload: ProfileSavePayload):
    """Save or replace a named Stream Deck profile."""
    from datetime import datetime, timezone
    name = (payload.name or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "Profile name required."}, status_code=400)
    if payload.deck_size not in (6, 15, 32):
        return JSONResponse({"ok": False, "error": "deck_size must be 6, 15, or 32."}, status_code=400)
    overrides_json = json.dumps(payload.key_overrides)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db = SessionLocal()
    try:
        row = db.query(StreamDeckProfile).filter(StreamDeckProfile.name == name).first()
        if row:
            row.deck_size = payload.deck_size
            row.key_overrides = overrides_json
            row.updated_at = now
        else:
            row = StreamDeckProfile(
                name=name,
                deck_size=payload.deck_size,
                key_overrides=overrides_json,
                created_at=now,
                updated_at=now,
            )
            db.add(row)
        db.commit()
        return {"ok": True, "profile": _profile_to_dict(row)}
    finally:
        db.close()


@router.delete("/streamdeck/profiles/{name}")
async def streamdeck_profiles_delete(name: str):
    """Delete a named Stream Deck profile."""
    db = SessionLocal()
    try:
        row = db.query(StreamDeckProfile).filter(StreamDeckProfile.name == name).first()
        if not row:
            return JSONResponse({"ok": False, "error": "Profile not found."}, status_code=404)
        db.delete(row)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@router.get("/ap/status")
async def ap_status():
    """Whether the fallback Wi-Fi AP is active (Pi appliance only)."""
    if not is_raspberry_pi():
        return {"ok": True, "active": False}
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            r = (await c.get(f"{_HOST_BRIDGE}/ap/status")).json()
        return r
    except Exception:
        return {"ok": True, "active": False}


@router.post("/ap/disable")
async def ap_disable():
    """Stop the fallback hotspot after the user has configured Wi-Fi."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/ap/disable")
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


# Touch calibration
# ------------------
# These endpoints let the web UI stream raw touch events and apply a computed
# calibration matrix. Only meaningful on Pi Remote (venv service) where the
# process can access /dev/input/event* directly (service user in input group).

# Substrings of common touch-controller device names that do NOT contain the
# word "touch" (capacitive and resistive panels alike), so name-matching catches
# them too. The capability check below is the real backstop for anything unnamed.
_TOUCH_CONTROLLER_HINTS = (
    "touch", "ads7846", "goodix", "ft5x", "ft6", "edt-ft5", "edt_ft5", "ektf",
    "ilitek", "ili210", "ili251", "silead", "eeti", "egalax", "hynitron",
    "st1633", "gslx680", "stmpe", "raspberrypi-ts", "waveshare", "hosyond",
    "chipone", "icn85", "cst", "zforce",
)


def _block_handler(block: str) -> str | None:
    m = re.search(r"Handlers=.*?(event\d+)", block)
    return "/dev/input/" + m.group(1) if m else None


def _looks_like_touchscreen(block: str) -> bool:
    """True when a /proc/bus/input/devices block is a touchscreen.

    Two signals, either is enough: the device name matches a known controller
    (covers panels that do not say "touch"), or the kernel flags it as a direct
    absolute pointer, i.e. INPUT_PROP_DIRECT (PROP bit 1) plus absolute axes.
    The capability check is what catches an oddly named or generic panel."""
    low = block.lower()
    if any(hint in low for hint in _TOUCH_CONTROLLER_HINTS):
        return True
    prop = re.search(r"^B: PROP=([0-9a-fA-F]+)", block, re.M)
    has_abs = re.search(r"^B: ABS=[0-9a-fA-F]", block, re.M) is not None
    # INPUT_PROP_DIRECT (bit 1) marks a screen-mapped device (a touchscreen),
    # versus an indirect one like a touchpad; require absolute axes too.
    return bool(prop) and bool(int(prop.group(1), 16) & 0x2) and has_abs


def _find_touch_device() -> str | None:
    """Return the first /dev/input/eventN that looks like a touchscreen."""
    try:
        blocks = open("/proc/bus/input/devices").read().split("\n\n")
    except OSError:
        return None
    for b in blocks:
        if _looks_like_touchscreen(b):
            h = _block_handler(b)
            if h:
                return h
    return None


# Linux input-event constants (linux/input-event-codes.h).
_EV_KEY = 0x01
_EV_ABS = 0x03
_ABS_X = 0x00
_ABS_Y = 0x01
_BTN_TOUCH = 0x14A
# struct input_event: a timeval (two longs) then type/code (u16) and value
# (s32). Native sizing matches the running kernel's word size (8-byte longs on
# a 64-bit kernel, 4-byte on 32-bit), so this adapts without per-arch branching.
_INPUT_EVENT_FORMAT = "llHHi"
_INPUT_EVENT_SIZE = struct.calcsize(_INPUT_EVENT_FORMAT)
# struct input_absinfo: six s32 (value, min, max, fuzz, flat, resolution).
_ABSINFO_FORMAT = "6i"
_ABSINFO_SIZE = struct.calcsize(_ABSINFO_FORMAT)


def _eviocgabs(axis: int) -> int:
    """ioctl request number for EVIOCGABS(axis): _IOR('E', 0x40+axis, absinfo)."""
    return (2 << 30) | (_ABSINFO_SIZE << 16) | (ord("E") << 8) | (0x40 + axis)


def _abs_range(fd: int, axis: int, default_max: int = 4095) -> tuple[int, int]:
    """Return (min, max) for an absolute axis via EVIOCGABS, or a sane default."""
    try:
        buf = bytearray(_ABSINFO_SIZE)
        fcntl.ioctl(fd, _eviocgabs(axis), buf, True)
        _value, minimum, maximum, _fuzz, _flat, _res = struct.unpack(_ABSINFO_FORMAT, bytes(buf))
        if maximum > minimum:
            return minimum, maximum
    except OSError:
        pass
    return 0, default_max


async def _evtest_sse(device: str):
    """Stream touch axis ranges and taps from a kernel input device as SSE.

    Reads the input device directly in Python rather than shelling out to
    evtest, so it needs no extra binary in the image (the appliance container
    mounts /dev/input). A background thread does the blocking reads and hands
    completed taps to the async generator over a queue. Same shape as before: a
    ranges event first, then a tap event on each BTN_TOUCH release."""
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as e:
        yield "data: " + json.dumps({"type": "error", "msg": f"cannot open {device}: {e}"}) + "\n\n"
        return

    x_min, x_max = _abs_range(fd, _ABS_X)
    y_min, y_max = _abs_range(fd, _ABS_Y)
    yield "data: " + json.dumps({
        "type": "ranges", "x_min": x_min, "x_max": x_max,
        "y_min": y_min, "y_max": y_max,
    }) + "\n\n"

    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()

    def _read_loop():
        x = y = None
        try:
            while not stop.is_set():
                # select with a timeout so the thread checks `stop` and exits
                # promptly when the client disconnects, instead of blocking on a
                # read until the next physical touch.
                r, _, _ = select.select([fd], [], [], 0.5)
                if not r:
                    continue
                try:
                    data = os.read(fd, _INPUT_EVENT_SIZE * 64)
                except OSError:
                    break
                for off in range(0, len(data) - _INPUT_EVENT_SIZE + 1, _INPUT_EVENT_SIZE):
                    _s, _u, etype, code, value = struct.unpack(
                        _INPUT_EVENT_FORMAT, data[off:off + _INPUT_EVENT_SIZE])
                    if etype == _EV_ABS and code == _ABS_X:
                        x = value
                    elif etype == _EV_ABS and code == _ABS_Y:
                        y = value
                    elif etype == _EV_KEY and code == _BTN_TOUCH and value == 0 \
                            and x is not None and y is not None:
                        loop.call_soon_threadsafe(queue.put_nowait, (x, y))
                        x = y = None
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    t = threading.Thread(target=_read_loop, daemon=True)
    t.start()
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            tx, ty = item
            yield "data: " + json.dumps({"type": "tap", "x": tx, "y": ty}) + "\n\n"
    finally:
        stop.set()
        try:
            os.close(fd)
        except OSError:
            pass


# The kiosk on the Pi watches for this flag and, when present, navigates its own
# display to the fullscreen calibration page. That is how a "Calibrate" click in
# a remote browser starts the tap sequence on the Pi's physical touchscreen
# (where the person actually is) instead of on the remote browser.
_CAL_FLAG = Path(settings.data_dir) / "calibrate_touch.flag"


@router.post("/calibrate/touch/request")
async def calibrate_touch_request():
    """Signal the kiosk to launch the fullscreen calibration page on its display."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    if not _find_touch_device():
        return JSONResponse({"ok": False, "error": "No touch device detected on this Pi."})
    try:
        _CAL_FLAG.parent.mkdir(parents=True, exist_ok=True)
        _CAL_FLAG.write_text("1")
        # Drop any leftover cancel/done from a previous run so they do not abort
        # or prematurely clear this one.
        _CAL_CANCEL_FLAG.unlink(missing_ok=True)
        _CAL_DONE_FLAG.unlink(missing_ok=True)
    except OSError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    return {"ok": True, "message": "Calibration started on the Pi touchscreen."}


@router.get("/calibrate/touch/pending")
async def calibrate_touch_pending():
    """Polled by the kiosk page; true once a remote browser asks to calibrate."""
    return {"pending": _CAL_FLAG.exists()}


# Cancelling calibration belongs on the REMOTE browser, not the Pi touchscreen:
# the panel is uncalibrated during the test, so a Cancel button on it is hard to
# hit. The remote UI sets this flag; the fullscreen calibration page polls it and
# returns to the dashboard. One-shot, cleared on read, same pattern as above.
_CAL_CANCEL_FLAG = Path(settings.data_dir) / "calibrate_cancel.flag"


@router.post("/calibrate/touch/cancel")
async def calibrate_touch_cancel():
    """From the remote UI: ask the Pi calibration page to stop and go back."""
    try:
        _CAL_CANCEL_FLAG.parent.mkdir(parents=True, exist_ok=True)
        _CAL_CANCEL_FLAG.write_text("1")
        # If the kiosk never reached the calibration page, also clear the start
        # flag so a queued request does not fire later.
        _CAL_FLAG.unlink(missing_ok=True)
    except OSError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    return {"ok": True}


@router.get("/calibrate/touch/cancel/pending")
async def calibrate_touch_cancel_pending():
    """Polled by the Pi calibration page; true once the remote asks to cancel."""
    pending = _CAL_CANCEL_FLAG.exists()
    if pending:
        try:
            _CAL_CANCEL_FLAG.unlink()
        except OSError:
            pass
    return {"pending": pending}


@router.post("/calibrate/touch/reset")
async def calibrate_touch_reset():
    """Remove the calibration matrix (revert to the panel default), via the
    host bridge. Used to recover from a calibration that came out wrong."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    try:
        async with httpx.AsyncClient(timeout=40.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/touch/calibrate/reset")
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


# Kiosk navigate-to-dashboard
# ---------------------------
# When the setup wizard is finished from a remote browser, the Pi's attached
# kiosk display is still sitting on the wizard page. It cannot navigate itself,
# so the wizard sets this flag and the kiosk poller (in base.html) picks it up
# and drives its own display to the dashboard. Same one-shot flag pattern as the
# touch-calibration flow above.
_KIOSK_NAV_FLAG = Path(settings.data_dir) / "kiosk_navigate.flag"


@router.post("/kiosk/navigate/request")
async def kiosk_navigate_request():
    """Signal the kiosk to leave the wizard and load the dashboard.

    Called by the wizard once setup saves successfully. Best effort: if the flag
    cannot be written we report it but never block finishing setup.
    """
    try:
        _KIOSK_NAV_FLAG.parent.mkdir(parents=True, exist_ok=True)
        _KIOSK_NAV_FLAG.write_text("1")
    except OSError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    return {"ok": True}


@router.get("/kiosk/navigate/pending")
async def kiosk_navigate_pending():
    """Polled by the kiosk page; true once setup has finished. One-shot.

    Clears the flag as it reports it so the kiosk navigates exactly once and the
    poll does not loop the display back to the dashboard on every tick.
    """
    pending = _KIOSK_NAV_FLAG.exists()
    if pending:
        try:
            _KIOSK_NAV_FLAG.unlink()
        except OSError:
            pass
    return {"pending": pending}


@router.get("/calibrate/touch/page", response_class=HTMLResponse)
async def calibrate_touch_page(request: Request):
    """Fullscreen calibration page the kiosk navigates to. Clears the flag.

    The active output rotation is passed in so the page can compensate for it:
    wlroots applies the output transform to touch input as well as the display,
    so the calibration matrix (applied before that transform) must be fit in the
    pre-transform space.
    """
    try:
        _CAL_FLAG.unlink()
    except OSError:
        pass
    rotation = 0
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            data = (await c.get(f"{_HOST_BRIDGE}/display/rotation")).json()
        if data.get("ok"):
            rotation = int(data.get("rotation", 0) or 0)
    except Exception:
        rotation = 0
    return templates.TemplateResponse(
        request, "calibrate.html", {"rotation": rotation})


@router.get("/calibrate/touch/events")
async def calibrate_touch_events():
    """SSE stream of raw ABS_X/ABS_Y touch events from the kernel input layer.

    First event: {"type": "ranges", "x_min": 0, "x_max": 4095, ...}
    Subsequent: {"type": "tap", "x": int, "y": int} on each BTN_TOUCH release.
    """
    if not is_raspberry_pi():
        return JSONResponse({"error": "Not available on this platform."}, status_code=400)
    device = _find_touch_device()
    if not device:
        return JSONResponse({"error": "No touch device found."}, status_code=400)
    return StreamingResponse(
        _evtest_sse(device),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


class TouchMatrixPayload(BaseModel):
    matrix: str = ""


@router.post("/calibrate/touch/apply")
async def calibrate_touch_apply(payload: TouchMatrixPayload):
    """Write a LIBINPUT_CALIBRATION_MATRIX via the host bridge.

    The app service runs with privileges capped to CAP_NET_BIND_SERVICE, so it
    cannot write to /etc/udev/rules.d or run udevadm itself (and sudo fails
    under that cap bound). The host bridge runs as root and applies the matrix.
    """
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    parts = payload.matrix.strip().split()
    if len(parts) != 6:
        return JSONResponse({"ok": False, "error": "Matrix must be exactly 6 floats."})
    try:
        [float(p) for p in parts]
    except ValueError:
        return JSONResponse({"ok": False, "error": "Non-numeric value in matrix."})
    matrix_str = " ".join(parts)
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/touch/calibrate",
                             json={"matrix": matrix_str})
        data = r.json()
        if data.get("ok"):
            # Signal the remote browser that calibration finished, so it can
            # clear the Cancel control (the kiosk restarts here and cannot report
            # back itself). One-shot flag, read+cleared by the done poll below.
            try:
                _CAL_DONE_FLAG.write_text("1")
                _CAL_CANCEL_FLAG.unlink(missing_ok=True)
            except OSError:
                pass
            return {"ok": True, "message": data.get("message", ""),
                    "kiosk_restarted": data.get("kiosk_restarted", False)}
        return JSONResponse({"ok": False, "error": data.get("error", f"HTTP {r.status_code}")})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


_CAL_DONE_FLAG = Path(settings.data_dir) / "calibrate_done.flag"


@router.get("/calibrate/touch/done/pending")
async def calibrate_touch_done_pending():
    """Polled by the remote UI; true once a calibration has been applied. The
    flag is one-shot so the Cancel control clears exactly once."""
    pending = _CAL_DONE_FLAG.exists()
    if pending:
        try:
            _CAL_DONE_FLAG.unlink()
        except OSError:
            pass
    return {"pending": pending}


# Backwards-compatible alias for the old endpoint name
@router.post("/test/vision")
async def test_vision_legacy(payload: dict):
    provider = payload.get("vision_provider") or payload.get("provider", "")
    key_field = f"{provider}_api_key"
    return await test_provider(TestProviderPayload(
        provider=provider,
        api_key=payload.get(key_field, payload.get("api_key", "")),
        model=payload.get(f"{provider}_model", payload.get("model", "")),
        base_url=payload.get("ollama_base_url", payload.get("base_url", "")),
    ))
