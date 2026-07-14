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
from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from ..config import (
    settings, APP_NAME, APP_VERSION, GITHUB_REPO, THEMES, _DEFAULT_THEME,
    UI_SCALES, _DEFAULT_UI_SCALE,
    DISPLAY_ROTATIONS, _DEFAULT_DISPLAY_ROTATION,
    DISPLAY_TYPES, _DEFAULT_DISPLAY_TYPE,
    HARDWARE_PRESETS,
    FLOATING_NAV_POSITIONS,
    FLOATING_NAV_ORIENTATIONS,
    NAV_VISIBILITY,
    TIMER_CHIPS,
    COMMON_TIMEZONES, format_local,
    CLOCK_FORMATS, _DEFAULT_CLOCK_FORMAT,
    SCREENSAVER_MODES, _DEFAULT_SCREENSAVER_MODE,
    STREAMDECK_KEY_STYLES, STREAMDECK_ICON_COLORS,
    DEPLOYMENT_MODES, _DEFAULT_DEPLOYMENT_MODE,
    AI_MODELS, SATELLITE_PULL_FIELDS,
    KITCHEN_APPLIANCES, KITCHEN_APPLIANCE_KEYS,
    browser_host, device_hostname,
    resolve_custom_colors, active_custom_name,
)
from ..database import SessionLocal
from ..dependencies import reset_providers
from ..ingress import ingress_redirect
from ..hardware import is_raspberry_pi, board_model, supports_local_stack
from ..passwords import verify_secret
from ..models.db_models import StreamDeckProfile
from ..navigation import all_tabs, default_tabs, normalize_custom_tabs, NAV_TABS, CUSTOM_PREFIX
from ..services.bridge import bridge_client
from ..services import tunnel_local
from ..services.host_panel import mealie_action_state, classify_active_connection
from ..services import status_summary
from ..storage_categories import custom_categories, _normalize_custom, storable
from ..templating import templates

router = APIRouter(prefix="/setup", tags=["setup"])

# Saved values for these are never rendered back into the page. The form
# sends "" to keep the stored value and "__CLEAR__" to erase it.
_SECRET_FIELDS = [
    "gemini_api_key", "openai_api_key", "anthropic_api_key",
    "grocy_api_key", "mealie_api_key",
    "themealdb_api_key", "spoonacular_api_key",
    "auth_password", "viewer_password", "api_key", "upstream_api_key", "kiosk_pin",
    "streamdeck_ha_token", "immich_api_key",
]
_CLEAR = "__CLEAR__"


def _clean_custom_nav_tabs(submitted) -> list[dict]:
    """Validate posted custom nav tabs into clean stored dicts.

    Runs the same normalizer the renderer uses, then re-projects to the stored
    shape {id,label,icon,url,parent} so settings.json holds only valid entries
    with stable, de-duplicated ids. Invalid rows are silently dropped.
    """
    cleaned = normalize_custom_tabs(submitted if isinstance(submitted, list) else [])
    return [{"id": t["key"], "label": t["label"], "icon": t["icon"],
             "url": t["href"], "parent": t.get("parent", ""),
             "heading": bool(t.get("heading"))} for t in cleaned]


def _clean_nav_parents(submitted) -> dict:
    """Keep only built-in child->parent string pairs from a posted map.

    Custom tabs carry their own parent field, so this map covers built-ins only.
    A child or parent that is not a known built-in key, or a self-reference, is
    dropped so a stale or hand-crafted value never breaks the nav tree.
    """
    if not isinstance(submitted, dict):
        return {}
    keys = {t["key"] for t in NAV_TABS}
    out: dict = {}
    for child, parent in submitted.items():
        child, parent = str(child), str(parent or "").strip()
        # The child is always a built-in tab. The parent may be another built-in
        # OR a custom heading/folder (a custom_-prefixed key the editor created),
        # so a built-in page can be filed under a user-made folder.
        parent_ok = parent in keys or parent.startswith(CUSTOM_PREFIX)
        if child in keys and parent_ok and child != parent:
            out[child] = parent
    return out


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
    ai_token_budget: int = 0
    scanner_type: str = ""
    barcode_global_capture: bool = True
    quiet_mode: bool = False
    barcode_enrichment: str = "llm"
    enrich_provider: str = ""
    enrich_model: str = ""
    llm_expiry_enabled: bool = False
    grocy_base_url: str = ""
    grocy_api_key: str = ""
    grocy_public_url: str = ""
    device_hostname: str = ""
    qr_url_mode: str = "auto"
    qr_public_url: str = ""
    mealie_base_url: str = ""
    mealie_api_key: str = ""
    mealie_public_url: str = ""
    recipe_source: str = "themealdb"
    # Where the shopping list lives: "" automatic | "grocy" | "mealie"
    # (services/shopping_source.py documents the automatic rule).
    shopping_backend: str = ""
    themealdb_api_key: str = ""
    spoonacular_api_key: str = ""
    staple_items: str = ""
    perishable_days: int = 14
    expiring_soon_days: int = 5
    suggest_per_tier: int = 8
    nav_order: str = ""
    nav_hidden: str = ""
    # Custom nav tabs + built-in nesting map (FoodAssistant-9gdz). None = the
    # field was not submitted, so the stored value is left alone (see handler).
    custom_nav_tabs: list[dict] | None = None
    nav_parents: dict | None = None
    ui_theme: str = _DEFAULT_THEME
    # Custom theme builder swatches (FoodAssistant-hatd). Declared so they
    # round-trip through /save (BaseModel drops undeclared fields).
    custom_theme_base: str = "dark"
    custom_theme_primary: str = "#4f9dff"
    custom_theme_accent: str = "#34d399"
    custom_theme_bg: str = "#0d1117"
    custom_theme_surface: str = "#161b22"
    custom_theme_text: str = "#e6edf3"
    # Background image (FoodAssistant-e2t6). The URL round-trips through /save
    # for the external-URL case; uploads use the dedicated /setup/background
    # endpoint. Opacity is the image layer's 0-100 visibility.
    background_image_url: str = ""
    background_opacity: int = 40
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
    # Deck rotation, saved so a hardware preset or the deck editor can persist it
    # (FoodAssistant-kl5n). Round-trips through /save; the handler clamps it to a
    # supported angle and the push path stamps it into the deck config.
    streamdeck_rotation: int = 0
    start_page_enabled: bool = False
    # Start Page mode (FoodAssistant-gg33): "glance" auto-builds the home from the
    # top-level nav pages plus notification pills; "custom" uses the arranged
    # layout. Validated to one of those two in the save handler.
    start_page_mode: str | None = None
    start_page_keys: int = 15
    start_page_layout: list | None = None
    # Custom-key definitions built in the Start Page editor; merged into the
    # shared streamdeck_key_overrides store (deck slots preserved) by the handler.
    start_custom_defs: list | None = None
    start_loaded_ids: list | None = None
    # These were previously sent by the setup page but dropped here (BaseModel
    # ignores unknown fields), so idle timeouts, key overrides, and the Stream
    # Deck weather never persisted through /save. Declared so they round-trip.
    streamdeck_idle_timeout: int = 0
    display_idle_timeout: int = 0
    kiosk_auto_home_enabled: bool | None = None
    kiosk_auto_home_seconds: int | None = None
    kiosk_auto_home_exempt: str | None = None
    screensaver_minutes: int = 0
    screensaver_speed: str | None = None
    screensaver_pill_scale: str | None = None
    screensaver_mode: str | None = None
    screensaver_photo_seconds: int | None = None
    screensaver_ken_burns: bool | None = None
    screensaver_ken_burns_speed: str | None = None
    # Screensaver photo source (FoodAssistant-af1l): where the slideshow gets
    # its pictures. immich_api_key is a secret (blank keeps the stored one).
    photo_source: str | None = None
    photo_folder: str | None = None
    immich_base_url: str | None = None
    immich_api_key: str = ""
    immich_album_id: str | None = None
    photo_urls: str | None = None
    screensaver_all_clients: bool = False
    streamdeck_logo_when_display_off: bool = True
    osk_enabled: bool = True
    wake_on_motion: str = "auto"
    wake_on_presence: str = "auto"
    streamdeck_key_overrides: list = []
    streamdeck_weather_location: str = ""
    streamdeck_weather_units: str = "f"
    weather_api_base: str = ""
    streamdeck_key_style: str = ""
    streamdeck_icon_color: str = ""
    streamdeck_cameras: list = []
    streamdeck_ha_base_url: str = ""
    streamdeck_ha_token: str = ""
    streamdeck_ha_slots: list = []
    ha_events_enabled: bool = False
    ha_camera_popup_seconds: int = 20
    auto_update: bool = True
    update_channel: str = "main"
    convert_custom_rows: list = []
    floating_nav_position: str = ""
    floating_nav_orientation: str = ""
    floating_nav_autohide_streamdeck: bool = False
    nav_visibility: str = ""
    timer_chips: str = ""
    timezone: str = ""
    clock_format: str = ""
    scheduled_reboot_time: str = ""
    scheduled_reboot_frequency: str = ""
    scheduled_reboot_day: int = 0
    display_touch: bool = False
    auth_required: bool = True
    auth_password: str = ""
    current_password: str = ""  # required to change/remove an existing auth_password
    viewer_password: str = ""
    api_key: str = ""
    # Each row is {"key": <secret or __KEEP__:i>, "name": <label>}; a bare
    # string is still accepted (see _merge_satellite_keys).
    extra_api_keys: list[dict | str] | None = None
    # LAN device pairing (FoodAssistant-4box): whether a new satellite may ask
    # this server for its own API key with an on-screen confirmation code.
    local_device_pairing_enabled: bool = True
    rclone_remote: str = ""
    rclone_schedule_hours: int = 0
    usb_backup_interval_hours: int = 0
    # Label / document printing (FoodAssistant-fb8x). printing_enabled is the
    # master gate; the queue names come from the device's CUPS setup and the
    # size fields describe the label stock. All round-trip through /save.
    printing_enabled: bool = False
    label_printer_queue: str = ""
    document_printer_queue: str = ""
    # The fleet defaults the main server sets and other devices inherit
    # (FoodAssistant-7u7z). Without these here, /save silently drops them and
    # the server could never set a fleet default.
    fleet_label_printer_queue: str = ""
    fleet_document_printer_queue: str = ""
    label_width_in: float = 2.0
    label_height_in: float = 1.0
    label_dpi: int = 203
    # Bluetooth thermometers (FoodAssistant-mnks). The two toggles save from
    # the Thermometers pane; the device and entity lists are managed by the
    # /gadgets endpoints, not the settings form.
    gadgets_enabled: bool = False
    gadget_ha_enabled: bool = False


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
    """Check that a Pi Remote can reach the Pantry Raider server it controls."""
    url = (payload.remote_server_url or settings.remote_server_url).rstrip("/")
    if not url:
        return {"ok": False, "error": "Server URL is required."}
    try:
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
            r = await client.get(f"{url}/health")
        if r.status_code == 200:
            return {"ok": True, "message": f"Connected: Pantry Raider reachable at {url}"}
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


# --- Satellite side of LAN device pairing (FoodAssistant-4box) --------------
# The wizard's Main Server step can ask the server for this device's API key
# instead of the user copying one by hand. The browser cannot call the server
# directly (cross-origin), so these two endpoints relay: open the request,
# then poll its status until a logged-in user on the server approves. Both are
# in main.py's _SETUP_BYPASS because they run before this device is configured.

class PairingRelayPayload(BaseModel):
    server_url: str = ""
    request_id: str = ""


def _pairing_server_base(url: str) -> str:
    return (url or settings.remote_server_url or "").rstrip("/")


@router.post("/pairing/request")
async def pairing_request_relay(payload: PairingRelayPayload):
    """Ask the main server to pair this device; returns its code to display."""
    base = _pairing_server_base(payload.server_url)
    if not base:
        return {"ok": False, "error": "Enter the main server URL first."}
    import socket
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(f"{base}/api/pairing/request",
                                  json={"hostname": socket.gethostname()})
        data = r.json()
    except Exception as e:
        return {"ok": False, "error": _safe_error(e)}
    if not data.get("ok"):
        return {"ok": False, "error": data.get("error", f"HTTP {r.status_code} from the server.")}
    return {"ok": True, "request_id": data.get("request_id", ""),
            "code": data.get("code", ""), "expires_in": data.get("expires_in", 0)}


@router.post("/pairing/status")
async def pairing_status_relay(payload: PairingRelayPayload):
    """Poll the main server for the pairing decision. An approval carries the
    minted API key; the wizard fills upstream_api_key with it client-side and
    the normal save/sync path persists it."""
    base = _pairing_server_base(payload.server_url)
    if not base or not payload.request_id:
        return {"ok": False, "error": "Missing the server URL or request id."}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(f"{base}/api/pairing/status/{payload.request_id}")
        data = r.json()
    except Exception as e:
        return {"ok": False, "error": _safe_error(e)}
    if not data.get("ok"):
        return {"ok": False, "error": data.get("error", f"HTTP {r.status_code} from the server.")}
    out = {"ok": True, "status": data.get("status", "expired")}
    if data.get("status") == "approved":
        out["api_key"] = data.get("api_key", "")
    return out


# --- One-image mode switch (FoodAssistant-dzx9) ----------------------------
# A pi_hosted appliance can stand down its local Grocy/Mealie stack and run as
# a satellite of another server, and later switch back, without reflashing.
# The pure decision/settings logic lives in services/deployment_switch.py; the
# container stop/start is done by the host bridge (root). Nothing is deleted
# either way: the local inventory data stays on the device.

class SwitchToSatellitePayload(BaseModel):
    remote_server_url: str = ""
    upstream_api_key: str = ""


def _ensure_satellite_sync_task(request: Request) -> None:
    """Start the periodic satellite sync task after a runtime mode flip.

    The lifespan hook only starts it when the app BOOTS as a satellite, so a
    device switched at runtime needs it started here. Idempotent: an already
    running task is left alone.
    """
    from ..main import _periodic_satellite_sync
    task = getattr(request.app.state, "sync_task", None)
    if task is None or task.done():
        request.app.state.sync_task = asyncio.create_task(_periodic_satellite_sync())


@router.post("/deployment/to-satellite")
async def switch_to_satellite(payload: SwitchToSatellitePayload, request: Request):
    """Switch this pi_hosted appliance to satellite duty.

    Order matters for safety: validate everything and prove the main server
    accepts the key FIRST (nothing changed on failure), then park the local
    stack via the bridge, then flip the mode and pull the server's config. The
    pre-switch backend config is snapshotted so Switch Back restores it.
    """
    from ..services import deployment_switch as ds

    ok, err = ds.can_switch_to_satellite(settings.deployment_mode)
    if not ok:
        return {"ok": False, "error": err}
    ok, url_or_err = ds.validate_server_url(payload.remote_server_url)
    if not ok:
        return {"ok": False, "error": url_or_err}
    url = url_or_err
    key = payload.upstream_api_key or ""
    if not key or key == _CLEAR:
        key = settings.upstream_api_key or ""
    if not key:
        return {"ok": False, "error": "The main server's API key is required."}

    # Prove the link works before touching anything: the same endpoint the
    # satellite sync uses, with the same key.
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(f"{url}/api/config/satellite",
                                 headers={"X-API-Key": key})
        if r.status_code == 401:
            return {"ok": False, "error":
                    "The server rejected the API key. Copy it from the main "
                    "server's Settings under Security."}
        if r.status_code != 200:
            return {"ok": False, "error": f"The server answered HTTP {r.status_code}. "
                                          "Check the URL points at Pantry Raider."}
    except Exception as e:
        return {"ok": False, "error": f"Could not reach the server: {_safe_error(e, key)}"}

    # Snapshot the local backend config before the satellite sync overwrites it.
    snapshot = ds.hosted_snapshot(
        {f: getattr(settings, f, None) for f in SATELLITE_PULL_FIELDS})

    # Park the local stack (bridge, root). On failure nothing has changed yet.
    try:
        async with bridge_client(timeout=310.0) as client:
            r = await client.post(f"{_HOST_BRIDGE}/stack/standdown")
        body = r.json()
        if r.status_code != 200 or not body.get("ok"):
            return {"ok": False, "error": body.get(
                "error", "The local stack could not be stopped.")}
    except Exception as e:
        return {"ok": False, "error": f"Could not reach the device helper "
                                      f"to stop the local stack: {_safe_error(e, key)}"}

    settings.save(ds.satellite_switch_settings(url, key, snapshot))

    # First pull from the new main server, then keep syncing periodically.
    from ..services.satellite import sync_from_upstream
    result = await run_in_threadpool(sync_from_upstream)
    _ensure_satellite_sync_task(request)
    reset_providers()

    if result.get("ok"):
        return {"ok": True, "message":
                "This device is now a satellite. It pulled "
                f"{len(result.get('applied', []))} settings from the main "
                "server, and the local inventory stack is stopped with its "
                "data kept on this device."}
    return {"ok": True, "message":
            "This device is now a satellite and the local stack is stopped, "
            "but the first sync from the server failed: "
            f"{result.get('error', 'unknown error')}. It will retry "
            "automatically; you can also use Sync Now in Main Server settings."}


@router.post("/deployment/to-hosted")
async def switch_to_hosted(request: Request):
    """Switch a parked appliance back to running its own full stack.

    Only available on a device that was switched with the control above (a
    device flashed as a plain Pi Remote has no local stack and is refused).
    Starts the parked containers, then restores the snapshotted backend config.
    """
    from ..services import deployment_switch as ds

    ok, err = ds.can_switch_back(settings.deployment_mode,
                                 bool(settings.hosted_stack_parked))
    if not ok:
        return {"ok": False, "error": err}

    try:
        async with bridge_client(timeout=610.0) as client:
            r = await client.post(f"{_HOST_BRIDGE}/stack/standup")
        body = r.json()
        if r.status_code != 200 or not body.get("ok"):
            return {"ok": False, "error": body.get(
                "error", "The local stack could not be started.")}
    except Exception as e:
        return {"ok": False, "error": f"Could not reach the device helper "
                                      f"to start the local stack: {_safe_error(e)}"}

    snapshot = settings.hosted_config_snapshot \
        if isinstance(settings.hosted_config_snapshot, dict) else {}
    settings.save(ds.hosted_restore_settings(snapshot))
    # The backend panes are editable again: nothing is server-sourced now.
    object.__setattr__(settings, "server_sourced_fields", set())
    reset_providers()

    # Stop mirroring the old main server.
    task = getattr(request.app.state, "sync_task", None)
    if task is not None:
        task.cancel()
        request.app.state.sync_task = None

    return {"ok": True, "message":
            "This device is running its own inventory stack again. Grocy "
            "(and Mealie, if it was enabled) may take a minute to finish "
            "starting."}


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


def _printing_available() -> bool:
    """True when the device's print stack (CUPS / lp tools) is installed.

    Wrapped so the setup page never hard-fails if the printing service cannot
    import for any reason: a missing print stack just reads as unavailable."""
    try:
        from ..services.printing import printing_available
        return printing_available()
    except Exception:
        return False


def _pi_mdns_host() -> str:
    """Return the device's own browser host, e.g. '<hostname>.local'.

    Uses the resolved device hostname (a user override, the real host hostname
    from the host bridge, or socket.gethostname()), not a hardcoded name, so it
    works when several appliances share a LAN. Falls back to the LAN IP if no
    hostname is resolvable.
    """
    return browser_host()


def _setup_phone_url(request: Request) -> str:
    """A phone/PC-reachable URL to this device's setup page (FoodAssistant-cssj).

    The kiosk browser reaches the app over localhost, which is useless on a
    phone, so we swap in the device's LAN address and keep the port the request
    came in on. We prefer the LAN IP over the <hostname>.local mDNS name, because
    a phone or laptop on the same subnet can always reach the IP, while .local
    needs mDNS which many networks do not resolve (Pantry Raider). Off a Pi we
    fall back to the request hostname, which is already the address the user typed.
    """
    if is_raspberry_pi():
        from ..config import _lan_ip
        host = _lan_ip() or _pi_mdns_host()
    else:
        host = request.url.hostname or ""
    if not host:
        return ""
    port = request.url.port
    netloc = host if (not port or port in (80, 443)) else f"{host}:{port}"
    return f"http://{netloc}/setup"


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


def _system_timezone() -> str:
    """The host's current IANA timezone name for the "Auto (system)" label, or
    "" when it cannot be read. Best-effort: reads /etc/timezone, else the
    /etc/localtime symlink target."""
    try:
        tz = Path("/etc/timezone").read_text().strip()
        if tz:
            return tz
    except OSError:
        pass
    try:
        link = os.readlink("/etc/localtime")
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return ""


def _suggest_mealie_url(request: Request) -> str:
    """A suggested Mealie URL for opening in a BROWSER, or '' if not applicable.

    On a Pi appliance Mealie runs (or will run) on the same host at port 9285.
    Prefers the mDNS hostname so the link survives DHCP IP changes. This is for
    the "open in new tab" link, not the internal API field.
    Returns '' when Mealie is already configured or we are not on a Pi.
    """
    if not is_raspberry_pi():
        return ""
    if settings.mealie_base_url:
        return ""
    host = _pi_mdns_host()
    return f"http://{host}:9285" if host else ""


def _suggest_mealie_internal_url() -> str:
    """The suggested Mealie URL for the INTERNAL API field on a Pi appliance.

    The app talks to Mealie on the same host, so localhost is correct and always
    reachable container-to-host; a <hostname>.local or LAN address is only for a
    browser and may not resolve from inside the container (Pantry Raider). Empty
    when Mealie is already configured or off a Pi.
    """
    if not is_raspberry_pi() or settings.mealie_base_url:
        return ""
    return "http://localhost:9285"


def available_modes() -> dict:
    """Deployment modes offered on this host.

    On a Raspberry Pi we offer the two Pi modes and hide "Server hosted"
    (which targets a general server). Elsewhere only "Server hosted" applies.

    On a Pi we also drop Pi Hosted when the board is too weak to run the local
    stack (a low-tier family such as a Pi 3/Zero, or a board with less RAM than
    hardware.MIN_HOSTED_RAM_MB), leaving Pi Remote as the only offered Pi mode.
    Detection is deliberately conservative: an uncertain reading keeps both
    modes so a capable board is never restricted by a misdetect.
    """
    pi = is_raspberry_pi()
    modes = {k: v for k, v in DEPLOYMENT_MODES.items() if v["pi"] == pi}
    if pi and not supports_local_stack():
        modes = {k: v for k, v in modes.items() if not v["local_stack"]}
    return modes


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
    # Mealie card affordance (FoodAssistant-mvke): on a Pi appliance ask the
    # host bridge whether Mealie is already up so the card shows a running
    # indicator instead of the start button when it is. A satellite points at a
    # remote stack and never starts Mealie here, and any bridge hiccup falls
    # back to offering the start button (the prior behaviour), so this never
    # blocks the settings page.
    mealie_available = is_raspberry_pi() and not settings.is_satellite()
    mealie_state = ""
    if mealie_available:
        try:
            async with bridge_client(timeout=3.0) as c:
                ms = (await c.get(f"{_HOST_BRIDGE}/mealie/status")).json()
            if ms.get("ok"):
                mealie_state = ms.get("state", "")
        except Exception:
            mealie_state = ""
    mealie_action = mealie_action_state(
        installed=mealie_state in ("running", "starting"),
        running=mealie_state == "running",
        available=mealie_available,
    )
    # LAN link to the barcode-scanner setup wizard, encoded as a QR on the
    # Scanning pane so a user can open the wizard on a phone by the reader
    # instead of at the kiosk (FoodAssistant-udpk).
    from .qr import lan_url_for
    from ..services import recipe_source
    scanner_wizard_url = lan_url_for(request, "/ui/scanner-setup")
    return templates.TemplateResponse(request, "setup.html", {
        "request": request,
        "s": settings,
        "configured": settings.is_configured(),
        "scanner_wizard_url": scanner_wizard_url,
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
        "tabs_default": default_tabs(),
        # On-screen Start Page editor (Pantry Raider): the shared custom buttons
        # (same store as the Stream Deck). The built-in key catalog is the deck's
        # own (_sdCatalog, loaded client-side), so the two editors are identical.
        "start_customs": _start_customs(),
        "version": APP_VERSION,
        "custom_categories": custom_categories(),
        "themes": THEMES,
        # Saved named custom themes for the Theme dropdown, plus the colour set
        # and name to seed the builder from the active theme (FoodAssistant-nw49).
        "custom_themes": [t for t in (settings.custom_themes or []) if isinstance(t, dict) and t.get("id")],
        "active_custom": resolve_custom_colors(settings.ui_theme) or {
            "base": settings.custom_theme_base, "primary": settings.custom_theme_primary,
            "accent": settings.custom_theme_accent, "bg": settings.custom_theme_bg,
            "surface": settings.custom_theme_surface, "text": settings.custom_theme_text,
        },
        "active_custom_name": active_custom_name(),
        "ui_scales": UI_SCALES,
        "display_rotations": DISPLAY_ROTATIONS,
        "display_types": DISPLAY_TYPES,
        # Prebuilt hardware bundles (FoodAssistant-kl5n): the wizard's hardware
        # step and the Screen & Sleep pane render a "Start from a preset"
        # selector from this, and POST /setup/preset/apply saves the chosen one.
        "hardware_presets": HARDWARE_PRESETS,
        "suggested_grocy_url": suggested_grocy_url,
        "grocy_browser_link": grocy_browser_link,
        "suggested_mealie_url": _suggest_mealie_url(request),
        # Human-facing link to open a configured Mealie in a browser, resolved
        # the same way as the Grocy link (LAN address on a satellite).
        "mealie_browser_link": settings.mealie_link_url() if settings.mealie_base_url else "",
        "suggested_mealie_internal_url": _suggest_mealie_internal_url(),
        # Live Mealie affordance for the Pi host card: "start" | "running" |
        # "none" (FoodAssistant-mvke). "none" hides the whole action block.
        "mealie_action": mealie_action,
        "mealie_state": mealie_state,
        # Where the recipe library lives ("native" or "mealie"), for the
        # Recipes pane's library card and its migrate action.
        "recipes_backend_active": recipe_source.active_backend(),
        "deployment_modes": modes,
        "current_mode": current_mode,
        "is_pi": is_raspberry_pi(),
        "is_satellite": settings.is_satellite(),
        # Pi appliance (Pi Hosted or Pi Remote): both run the host bridge, so both
        # offer the in-app OTA update. Mode based so it is stable off-device too.
        "is_pi_appliance": settings.is_pi_appliance(),
        # For the Updates card's release-notes link.
        "github_repo": GITHUB_REPO,
        # Update-check bookkeeping + timezone (FoodAssistant-lq01/-amp0): the last
        # check shown pre-formatted in the configured zone, plus the tz options.
        "update_last_checked_display": format_local(
            settings.update_last_checked, settings.timezone,
            clock_format=settings.clock_format),
        "update_last_latest": settings.update_last_latest,
        "update_last_available": settings.update_last_available,
        "timezone": settings.timezone,
        "common_timezones": COMMON_TIMEZONES,
        "system_timezone": _system_timezone(),
        "clock_format": settings.clock_format,
        "scheduled_reboot_time": settings.scheduled_reboot_time,
        "scheduled_reboot_frequency": settings.scheduled_reboot_frequency,
        "scheduled_reboot_day": settings.scheduled_reboot_day,
        # Secrets the main server manages (pulled each sync). On a satellite these
        # render read-only; the device-local secrets (upstream key, password, PIN)
        # stay editable so the device can be paired or re-keyed (Pantry Raider).
        "satellite_managed": SATELLITE_PULL_FIELDS,
        "board_model": board_model(),
        # When True the board is a Pi too weak for the local stack, so Pi Hosted
        # was dropped from the picker; the template shows a short why-line.
        "hosted_unavailable": is_raspberry_pi() and not supports_local_stack(),
        "pi_mdns_host": _pi_mdns_host() if is_raspberry_pi() else "",
        # On the attached kiosk display the wizard's many text inputs are painful
        # to fill with a touchscreen, so when the page is opened in kiosk mode and
        # setup is not finished we steer the user to a phone/PC browser instead
        # (FoodAssistant-cssj). The reachable URL uses the device's LAN host, not
        # the kiosk's localhost, so a phone on the same network can open it.
        "kiosk": request.query_params.get("kiosk") == "1",
        "setup_phone_url": _setup_phone_url(request),
        # Default kitchen name for the Forager sign-in (the device's hostname,
        # so a signed-in account sees "kitchen-pi" rather than a blank).
        "suggested_kitchen_name": device_hostname(),
        # Portal signup link for the "Create an account" prompt by the Forager
        # sign-in fields (wizard and settings). The install's stable device_id
        # rides along as the install key so the free trial is limited to one
        # per install; device_id is an opaque per-install id, not personal data.
        # An older cloud that does not read it simply ignores the parameter.
        "cloud_signup_url": (settings.cloud_base_url.rstrip("/") + "/signup"
                             + (f"?install_key={settings.device_id}"
                                if settings.device_id else "")),
        # The domain a kitchen's chosen web address hangs off, shown as the
        # fixed suffix beside the Web address input (e.g. "forager.pantryraider.app").
        "tunnel_apex": settings.cloud_base_url.split("://", 1)[-1].split("/", 1)[0].strip().lower(),
        # Kitchen-appliance checklist, grouped for the Preferences section, with
        # each item's current checked state from the saved selection.
        "appliance_groups": _appliance_groups(),
        # Whether the device has a working print stack (CUPS / lp tools). The
        # Printing pane shows a calm "needs enabling on the device" note when
        # this is False (FoodAssistant-fb8x).
        "printing_available": _printing_available(),
    })


def _start_customs() -> list[dict]:
    from ..services import start_page
    return [{"id": c["id"], "label": c["label"], "icon": c["icon"],
             "color": c.get("color", "#374151"), "type": c["type"]}
            for c in start_page.custom_buttons()]


def _appliance_groups() -> dict:
    """Group the appliance catalog into major/minor/attachment with each item's
    checked state, so the Preferences checklist renders without logic in the
    template."""
    selected = set(settings.kitchen_appliances or [])
    groups: dict[str, list] = {"major": [], "minor": [], "attachment": []}
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


_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _theme_slug(name: str) -> str:
    """Stable id from a display name: lowercased, non-alphanumerics to '_'."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "theme"


class CustomThemePayload(BaseModel):
    name: str = ""
    base: str = "dark"
    primary: str = ""
    accent: str = ""
    bg: str = ""
    surface: str = ""
    text: str = ""


@router.post("/custom-theme")
async def save_custom_theme(payload: CustomThemePayload):
    """Save (or update) a named custom theme and make it active.

    Stores it in settings.custom_themes keyed by a slug of the name, then sets
    ui_theme to "custom:<id>". Resaving with the same name updates that theme.
    """
    name = (payload.name or "").strip()
    if not name:
        return {"ok": False, "error": "Give the theme a name."}
    base = payload.base if payload.base in ("light", "dark") else "dark"
    colors = {}
    fallback = {
        "primary": settings.custom_theme_primary, "accent": settings.custom_theme_accent,
        "bg": settings.custom_theme_bg, "surface": settings.custom_theme_surface,
        "text": settings.custom_theme_text,
    }
    for k in ("primary", "accent", "bg", "surface", "text"):
        v = (getattr(payload, k) or "").strip()
        if v and not _HEX_RE.match(v):
            return {"ok": False, "error": f"{k.title()} must be a #rrggbb colour."}
        colors[k] = v or fallback[k]
    theme_id = _theme_slug(name)
    entry = {"id": theme_id, "name": name, "base": base, **colors}
    existing = [t for t in (settings.custom_themes or []) if isinstance(t, dict) and t.get("id")]
    replaced = False
    for i, t in enumerate(existing):
        if t.get("id") == theme_id:
            existing[i] = entry
            replaced = True
            break
    if not replaced:
        existing.append(entry)
    settings.save({"custom_themes": existing, "ui_theme": f"custom:{theme_id}"})
    if is_raspberry_pi() and settings.has_streamdeck:
        from ..services.satellite import _push_streamdeck_settings
        _push_streamdeck_settings()
    return {"ok": True, "id": theme_id}


@router.post("/custom-theme/delete")
async def delete_custom_theme():
    """Delete the currently-active saved custom theme; fall back to the default."""
    name = getattr(settings, "ui_theme", "")
    if not (isinstance(name, str) and name.startswith("custom:")):
        return {"ok": False, "error": "No saved custom theme is active."}
    theme_id = name.split(":", 1)[1]
    remaining = [t for t in (settings.custom_themes or [])
                 if isinstance(t, dict) and t.get("id") and t.get("id") != theme_id]
    settings.save({"custom_themes": remaining, "ui_theme": _DEFAULT_THEME})
    return {"ok": True}


# -- Background image (FoodAssistant-e2t6) ----------------------------------

# Bitmap formats a browser renders as a CSS background, mapped to the on-disk
# extension we save them under. SVG is intentionally excluded: a background SVG
# can carry script, and it would be served same-origin.
_BG_TYPES = {
    "image/jpeg": ".jpg", "image/png": ".png",
    "image/webp": ".webp", "image/gif": ".gif",
}
_BG_MAX_BYTES = 8 * 1024 * 1024  # 8 MB: plenty for a full-screen photo.


def _bg_path() -> Path | None:
    """The stored background image file, or None if none is uploaded."""
    d = Path(settings.data_dir)
    for ext in (".jpg", ".png", ".webp", ".gif"):
        p = d / f"background{ext}"
        if p.exists():
            return p
    return None


@router.post("/background")
async def upload_background(file: UploadFile = File(...)):
    """Store an uploaded background image and point the setting at it.

    Saves to data_dir/background.<ext> (replacing any previous upload) and sets
    background_image_url to the internal serve route with a content hash for
    cache-busting, so a re-upload of a different image refreshes immediately.
    """
    ctype = (file.content_type or "").split(";", 1)[0].strip().lower()
    ext = _BG_TYPES.get(ctype)
    if not ext:
        return {"ok": False, "error": "Use a JPG, PNG, WebP, or GIF image."}
    data = await file.read()
    if not data:
        return {"ok": False, "error": "The uploaded file was empty."}
    if len(data) > _BG_MAX_BYTES:
        return {"ok": False, "error": "Image is larger than 8 MB."}
    import hashlib
    d = Path(settings.data_dir)
    d.mkdir(parents=True, exist_ok=True)
    # Remove any previous upload (possibly a different extension) so only one
    # background file ever exists on disk.
    for old in (".jpg", ".png", ".webp", ".gif"):
        op = d / f"background{old}"
        if op.exists():
            try:
                op.unlink()
            except OSError:
                pass
    (d / f"background{ext}").write_bytes(data)
    token = hashlib.sha256(data).hexdigest()[:12]
    settings.save({"background_image_url": f"setup/background/image?v={token}"})
    return {"ok": True, "url": settings.background_image_url}


@router.get("/background/image")
async def serve_background():
    """Serve the uploaded background image (FileResponse), or 404 if none."""
    p = _bg_path()
    if not p:
        return JSONResponse({"ok": False, "error": "no background"}, status_code=404)
    media = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp",
             "gif": "image/gif"}.get(p.suffix.lstrip("."), "application/octet-stream")
    return FileResponse(str(p), media_type=media)


@router.post("/background/clear")
async def clear_background():
    """Remove the background image (uploaded file and/or URL)."""
    p = _bg_path()
    if p:
        try:
            p.unlink()
        except OSError:
            pass
    settings.save({"background_image_url": ""})
    return {"ok": True}


@router.get("/ai-usage")
async def ai_usage():
    """Current AI token usage + budget for the AI settings panel (Pantry Raider).

    Also attaches an approximate dollar cost for this month and all time,
    priced with the currently selected provider's model (services/ai_pricing).
    The tracker stores combined input+output totals, so the figures are
    blended estimates; an unrecognised model gets no estimate at all.
    """
    from ..services import ai_pricing, usage
    data = usage.get_usage()
    provider = getattr(settings, "vision_provider", "") or ""
    model = getattr(settings, f"{provider}_model", "") or ""
    data["cost_model"] = model
    data["cost_month"] = ai_pricing.estimate_cost(data["month"], model)
    data["cost_total"] = ai_pricing.estimate_cost(data["total"], model)
    return {"ok": True, **data}


@router.post("/ai-usage/reset")
async def ai_usage_reset():
    """Clear the recorded AI token usage."""
    from ..services import usage
    usage.reset()
    return {"ok": True}


# ---- Forager pairing (docs/design/cloud-platform.md) ----------
# The install always dials out: a pairing code minted on the cloud portal is
# typed here, redeemed for a long-lived instance token, and stored. All three
# routes degrade honestly when the cloud is unreachable; none of them can
# break the settings page.

_CLOUD_TIMEOUT = httpx.Timeout(8.0, connect=5.0)
# Backups can be large and cross a real LLM-free but still slow upload path, so
# the push/pull gets a generous read timeout while the connect stays tight.
_CLOUD_BACKUP_TIMEOUT = httpx.Timeout(120.0, connect=6.0)


def _cloud_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.cloud_instance_token}",
        "X-Device-Version": APP_VERSION,
        "X-Device-Mode": settings.deployment_mode or "server",
    }


class CloudLinkPayload(BaseModel):
    code: str = ""


class CloudSigninPayload(BaseModel):
    email: str = ""
    password: str = ""
    device_name: str = ""
    # A code from the authenticator app (or a recovery code). Sent only when
    # the account has two-factor sign-in on and the cloud asked for it.
    totp: str = ""


def _apply_cloud_link(body: dict) -> dict:
    """Finish a fresh Forager link the same way for every sign-in path
    (password, Google, pairing code from the website).

    Stores the credential, and if no other AI provider is set up yet makes
    Forager the scanning and enrichment provider so scanning just works; an
    install with a working provider keeps it (the settings card offers a
    one-click switch instead). When the platform already knows this kitchen's
    public web address it is applied to qr_public_url so the local and
    outside addresses match; null or absent (no tunnel yet) leaves the
    stored value untouched.
    """
    to_save: dict = {"cloud_instance_token": body.get("instance_token", "")}
    providers_set = False
    if not settings.ai_configured():
        to_save["vision_provider"] = "cloud"
        to_save["enrich_provider"] = "cloud"
        providers_set = True
    public_url = (body.get("suggested_public_url") or "").strip().rstrip("/")
    if public_url:
        to_save["qr_public_url"] = public_url
    settings.save(to_save)
    reset_providers()
    return {"providers_set": providers_set, "public_url": public_url}


@router.post("/cloud/signin")
async def cloud_signin(payload: CloudSigninPayload):
    """Sign in with a Forager account and provision this install.

    One POST to the cloud's provision endpoint trades the account email and
    password for this install's own long-lived credential. The password is
    forwarded to the cloud and nowhere else: it is never saved, never logged
    (nothing in this app logs request bodies), and the error paths below
    scrub it so it cannot leak into a message.

    On success _apply_cloud_link finishes the link: providers when unset,
    the kitchen's public web address when the platform provides one.

    When the account has two-factor sign-in on, the cloud accepts the password
    but answers 401 with a machine-readable error: "totp_required" the first
    time (no code sent) and "totp_invalid" for a wrong code. Those come back
    with a totp_prompt flag so the page can ask for the code and resubmit.
    """
    email = payload.email.strip()
    password = payload.password
    totp = payload.totp.strip()
    if not email or not password:
        return {"ok": False, "error": "Enter your Forager email and password."}
    name = payload.device_name.strip() or device_hostname() or "Pantry Raider"
    base = settings.cloud_base_url.rstrip("/")
    body_out = {"email": email, "password": password, "device_name": name}
    if totp:
        body_out["totp"] = totp
    try:
        async with httpx.AsyncClient(timeout=_CLOUD_TIMEOUT) as client:
            r = await client.post(f"{base}/v1/instances/provision",
                                  json=body_out)
    except httpx.HTTPError as e:
        return {"ok": False, "error": (
            f"Forager could not be reached ({e.__class__.__name__}). "
            "Check the internet connection and try again.")}
    if r.status_code == 401:
        # A 2FA account answers with {"error": "totp_required"|"totp_invalid"};
        # a genuine bad password answers with {"detail": ...}. Tell them apart
        # so the page prompts for a code instead of blaming the password.
        try:
            gate = r.json().get("error", "")
        except ValueError:
            gate = ""
        if gate == "totp_required":
            return {"ok": False, "totp_prompt": True,
                    "error": ("Enter the code from your authenticator app to "
                              "finish signing in.")}
        if gate == "totp_invalid":
            return {"ok": False, "totp_prompt": True,
                    "error": ("That code did not match. Enter the current code "
                              "from your authenticator app (or a recovery "
                              "code) and try again.")}
        return {"ok": False, "error": ("That email and password did not match "
                                       "a Forager account. Check them and try "
                                       "again.")}
    if r.status_code == 429:
        return {"ok": False, "error": ("Too many sign-in attempts right now. "
                                       "Wait a minute and try again.")}
    if r.status_code != 200:
        try:
            detail = r.json().get("detail", "")
        except ValueError:
            detail = ""
        detail = detail if isinstance(detail, str) else ""
        return {"ok": False, "error": _safe_error(detail, password)
                or f"Forager answered with status {r.status_code}."}
    body = r.json() or {}
    if not body.get("instance_token"):
        return {"ok": False, "error": ("The sign-in worked but the reply was "
                                       "incomplete. Try again.")}
    applied = _apply_cloud_link(body)
    return {"ok": True,
            "account_email": body.get("account_email", email),
            "plan": body.get("plan", ""),
            **applied}


@router.get("/cloud/meta")
async def cloud_meta():
    """Forager feature discovery for the sign-in card, proxied so the browser
    never talks to the cloud directly.

    Drives the "Continue with Google" button: it renders only when the cloud
    is reachable and says Google sign-in is on. Unreachable or malformed
    replies degrade to every feature off, no button and no error, so the
    page never breaks over this.
    """
    base = settings.cloud_base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=_CLOUD_TIMEOUT) as client:
            r = await client.get(f"{base}/v1/meta")
        body = r.json() if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        body = {}
    body = body if isinstance(body, dict) else {}
    return {"ok": True, "oauth_google": bool(body.get("oauth_google")),
            "google_start_url": f"{base}/auth/google/start"}


@router.get("/cloud/oauth-return")
async def cloud_oauth_return(request: Request, code: str = "", flow: str = ""):
    """Land the browser after "Continue with Google" on the Forager site.

    Forager redirects here with a one-time code; redeeming it against the
    pairing endpoint yields this install's credential, and the link then
    finishes exactly like a password sign-in (_apply_cloud_link). The user
    always ends up back where they started, the settings AI pane or the
    setup wizard's AI step, with any failure carried as a friendly message
    in the cloud_error query parameter.
    """
    # flow=wizard came from the setup wizard (the hint rides through the
    # return_url the button built); everything else returns to settings.
    if flow == "wizard":
        dest, err_dest = "/setup?cloud=done", "/setup?cloud_error={msg}"
    else:
        dest = "/setup#pane-scanning"
        err_dest = "/setup?cloud_error={msg}#pane-scanning"

    def fail(msg: str):
        from urllib.parse import quote
        return ingress_redirect(request, err_dest.format(msg=quote(msg)))

    code = code.strip()
    if not code:
        return fail("Google sign-in did not finish. Try again.")
    base = settings.cloud_base_url.rstrip("/")
    name = device_hostname() or "Pantry Raider"
    try:
        async with httpx.AsyncClient(timeout=_CLOUD_TIMEOUT) as client:
            r = await client.post(f"{base}/v1/pairing/redeem",
                                  json={"code": code, "name": name})
    except httpx.HTTPError:
        return fail("Forager could not be reached. Check the internet "
                    "connection and try signing in again.")
    if r.status_code != 200:
        return fail("The Google sign-in expired or was already used. "
                    "Try signing in again.")
    body = r.json() or {}
    if not body.get("instance_token"):
        return fail("The sign-in worked but the reply was incomplete. "
                    "Try again.")
    _apply_cloud_link(body)
    return ingress_redirect(request, dest)


@router.post("/cloud/link")
async def cloud_link(payload: CloudLinkPayload):
    """Redeem a pairing code against the cloud and store the instance token."""
    code = payload.code.strip()
    if not code:
        return {"ok": False, "error": "Enter the pairing code from the cloud portal."}
    base = settings.cloud_base_url.rstrip("/")
    name = device_hostname() or "Pantry Raider"
    try:
        async with httpx.AsyncClient(timeout=_CLOUD_TIMEOUT) as client:
            r = await client.post(f"{base}/v1/pairing/redeem",
                                  json={"code": code, "name": name})
    except httpx.HTTPError as e:
        return {"ok": False, "error": _safe_error(
            f"Forager could not be reached ({e.__class__.__name__}). "
            "Check the internet connection and try again.")}
    if r.status_code != 200:
        try:
            detail = r.json().get("detail", "")
        except ValueError:
            detail = ""
        return {"ok": False, "error": detail or "The pairing code was not accepted."}
    token = (r.json() or {}).get("instance_token", "")
    if not token:
        return {"ok": False, "error": "The cloud reply had no instance token."}
    settings.save({"cloud_instance_token": token})
    reset_providers()
    return {"ok": True}


def cloud_convert_updates(vision_provider: str, enrich_provider: str,
                          tunnel_mode: str, tunnel_enabled: bool) -> dict:
    """Settings changes that switch a Forager-linked kitchen to self-hosted.

    Pure, so the convert flow and its test agree on exactly what changes. The
    Forager link is always forgotten. When Forager was the scanner (vision or
    enrichment provider "cloud"), it stops being the provider so the app does
    not keep claiming AI is ready against a service it just left: scanning is
    turned off until the user picks a self-hosted option (their own key or
    Ollama) rather than silently failing. A Forager web address / remote-access
    mode is turned off too. Nothing here touches inventory, recipes, other
    settings, or the device password: converting moves no data, it only changes
    which services this kitchen leans on.
    """
    updates: dict = {"cloud_instance_token": ""}
    if vision_provider == "cloud":
        # Fall back to the default provider, which has no key of its own, so
        # ai_configured() reads False and the AI-only UI hides until the user
        # sets up a self-hosted provider on the Scanning page.
        updates["vision_provider"] = "gemini"
    if enrich_provider == "cloud":
        updates["enrich_provider"] = ""
    # A legacy "subscription" mode reads as Forager; either way, clear it.
    if tunnel_mode in ("forager", "subscription"):
        updates["tunnel_mode"] = ""
    if tunnel_enabled:
        updates["tunnel_enabled"] = False
    return updates


@router.post("/cloud/unlink")
async def cloud_unlink():
    """Switch this kitchen to self-hosted: forget the Forager link and land in
    a working local state.

    The kitchen keeps everything that already lives on the device (inventory,
    recipes, every setting, the device password); only the cloud add-ons go.
    Steps, all best effort so an unreachable Forager never blocks the switch:

      1. If Forager remote access is on, take it down first (while the link is
         still live, so the Forager side is told too).
      2. Ask Forager to revoke this install's credential (DELETE /v1/instance);
         an unreachable or already-revoked service is fine, the account page
         can remove the install too.
      3. Save the local switch: forget the credential, and if Forager was the
         scanner, stop pointing scanning at it so the app does not offer AI
         actions that can no longer work (cloud_convert_updates).
    """
    # Turn off Forager remote access before dropping the link, so the cloud side
    # is notified while the credential still works. tunnel_enabled tracks the
    # Forager WireGuard tunnel specifically; a Cloudflare tunnel is self-hosted
    # and left untouched.
    if settings.tunnel_enabled:
        try:
            await tunnel_disable()
        except Exception:
            pass
    if settings.cloud_instance_token:
        base = settings.cloud_base_url.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=_CLOUD_TIMEOUT) as client:
                await client.delete(f"{base}/v1/instance",
                                    headers=_cloud_headers())
        except httpx.HTTPError:
            pass
    settings.save(cloud_convert_updates(
        settings.vision_provider, settings.enrich_provider,
        settings.tunnel_mode, settings.tunnel_enabled))
    reset_providers()
    return {"ok": True}


@router.get("/cloud/status")
async def cloud_status():
    """Linked state plus the account's plan and quota, for the AI pane.

    Proxies GET /v1/instance/me with the stored token so the browser never
    sees the token itself. Unreachable or rejected replies come back as data,
    never as an error status: the settings page must render regardless.
    """
    if not settings.cloud_linked():
        return {"ok": True, "linked": False}
    base = settings.cloud_base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=_CLOUD_TIMEOUT) as client:
            r = await client.get(f"{base}/v1/instance/me",
                                 headers=_cloud_headers())
    except httpx.HTTPError as e:
        return {"ok": True, "linked": True, "reachable": False,
                "error": _safe_error(
                    f"Forager could not be reached ({e.__class__.__name__}).",
                    settings.cloud_instance_token)}
    if r.status_code == 401:
        return {"ok": True, "linked": True, "reachable": True, "valid": False,
                "error": ("Forager no longer recognizes this device. "
                          "Disconnect, then sign in again.")}
    if r.status_code != 200:
        return {"ok": True, "linked": True, "reachable": True, "valid": False,
                "error": f"Forager answered with status {r.status_code}."}
    body = r.json() or {}
    return {"ok": True, "linked": True, "reachable": True, "valid": True,
            "instance_id": body.get("instance_id"),
            "name": body.get("name", ""),
            "account_email": body.get("account_email", ""),
            "entitlement": body.get("entitlement", {})}


# ---- Back up to Forager (FoodAssistant-kzjz) ----
# A Premium account can push this install's data-directory zip to Forager cloud
# storage and pull it back. The cloud is the source of truth: it re-checks
# Premium on every call, so these endpoints treat "not Premium" as a normal
# data reply and the UI gate is convenience only. Everything is fail-soft: not
# linked, not Premium, or the cloud being down comes back as a clear message,
# and the local backup download still works.

@router.get("/cloud/backup/status")
async def cloud_backup_status():
    """Whether cloud backup is available here (linked and Premium) and the
    account's stored backups, for the Backups pane.

    Reuses the cloud's own Premium gate: GET /v1/backup/list answers 200 for a
    Premium account and 402 otherwise, so the app never has to trust a client
    guess. Unreachable or rejected replies come back as data, never an error
    status: the settings page must render regardless.
    """
    if not settings.cloud_linked():
        return {"ok": True, "linked": False, "premium": False, "backups": []}
    base = settings.cloud_base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=_CLOUD_TIMEOUT) as client:
            r = await client.get(f"{base}/v1/backup/list",
                                 headers=_cloud_headers())
    except httpx.HTTPError as e:
        return {"ok": True, "linked": True, "reachable": False, "premium": False,
                "backups": [], "error": _safe_error(
                    f"Forager could not be reached ({e.__class__.__name__}).",
                    settings.cloud_instance_token)}
    if r.status_code in (402, 403):
        # Linked, but the account is not Premium: the feature is locked.
        return {"ok": True, "linked": True, "reachable": True, "premium": False,
                "backups": []}
    if r.status_code == 401:
        return {"ok": True, "linked": True, "reachable": True, "premium": False,
                "backups": [], "error": ("Forager no longer recognizes this "
                                         "device. Disconnect, then sign in "
                                         "again.")}
    if r.status_code != 200:
        return {"ok": True, "linked": True, "reachable": True, "premium": False,
                "backups": [], "error": f"Forager answered with status {r.status_code}."}
    body = r.json() or {}
    return {"ok": True, "linked": True, "reachable": True, "premium": True,
            "backups": body.get("backups", [])}


@router.post("/cloud/backup/upload")
async def cloud_backup_upload(include_secrets: bool = False):
    """Zip this install's data directory and store it in Forager.

    Builds the archive with the same builder as the local backup download, then
    POSTs it to the cloud. Premium is enforced on the cloud, so a non-Premium
    account gets a clear upgrade message here rather than a stored backup.
    """
    if not settings.cloud_linked():
        return {"ok": False, "error": ("This install is not linked to Forager. "
                                       "Sign in under Settings, AI first.")}
    from .admin import _build_zip
    zip_bytes, filename = _build_zip(include_secrets=include_secrets)
    base = settings.cloud_base_url.rstrip("/")
    files = {"file": (filename, zip_bytes, "application/zip")}
    try:
        async with httpx.AsyncClient(timeout=_CLOUD_BACKUP_TIMEOUT) as client:
            r = await client.post(f"{base}/v1/backup/upload", files=files,
                                  headers=_cloud_headers())
    except httpx.HTTPError as e:
        return {"ok": False, "error": (
            f"Forager could not be reached ({e.__class__.__name__}). Your "
            "local backup download still works.")}
    if r.status_code in (402, 403):
        return {"ok": False, "premium": False, "error": (
            "Back up to Forager is a Premium feature. Upgrade your plan to turn "
            "it on. You can still download a local backup below.")}
    if r.status_code == 413:
        return {"ok": False, "error": ("This backup is too large to store in "
                                       "Forager. Download a local backup "
                                       "instead.")}
    if r.status_code != 200:
        return {"ok": False,
                "error": f"Forager could not store the backup (status {r.status_code})."}
    return {"ok": True, "backup": r.json() or {},
            "message": "Backed up to Forager."}


class CloudRestorePayload(BaseModel):
    # Which stored backup to restore; the newest is used when left unset.
    backup_id: int | None = None
    restore_password: str = ""


@router.post("/cloud/backup/restore")
async def cloud_backup_restore(payload: CloudRestorePayload):
    """Download a backup from Forager and run it through the existing guarded
    restore.

    The device password is required exactly like a local restore (the same gate
    admin.restore uses), so a walk-up at an open Settings page cannot pull the
    cloud backup down over the running data.
    """
    if not settings.cloud_linked():
        return {"ok": False, "error": "This install is not linked to Forager."}
    from .admin import _require_current_password, _restore_zip
    try:
        _require_current_password(
            payload.restore_password,
            "Enter your current password to restore a backup.")
    except HTTPException as e:
        return {"ok": False, "error": e.detail}
    base = settings.cloud_base_url.rstrip("/")
    path = (f"/v1/backup/download/{int(payload.backup_id)}"
            if payload.backup_id else "/v1/backup/latest")
    try:
        async with httpx.AsyncClient(timeout=_CLOUD_BACKUP_TIMEOUT) as client:
            r = await client.get(f"{base}{path}", headers=_cloud_headers())
    except httpx.HTTPError as e:
        return {"ok": False, "error": (
            f"Forager could not be reached ({e.__class__.__name__}). Your data "
            "was not changed.")}
    if r.status_code in (402, 403):
        return {"ok": False, "premium": False, "error": (
            "Restore from Forager is a Premium feature. Upgrade your plan to "
            "turn it on.")}
    if r.status_code == 404:
        return {"ok": False, "error": "No Forager backup was found to restore."}
    if r.status_code != 200:
        return {"ok": False,
                "error": f"Forager could not return the backup (status {r.status_code})."}
    try:
        result = _restore_zip(r.content)
    except HTTPException as e:
        return {"ok": False, "error": e.detail}
    return {"ok": True, **result}


# ---- Forager remote access (WireGuard hub tunnel, FoodAssistant-uczr) ----
# Remote access gives the kitchen a web address reachable from anywhere through
# Forager. The WireGuard endpoint lives on the Pi via the host bridge (only
# root can create the interface); this app orchestrates it: keygen on the
# bridge, enable on the cloud, then bring the interface up with what the cloud
# returns. Every failure rolls back and reports honestly, and no key material
# ever passes through the app (the private key stays on the device).

_TUNNEL_UPGRADE_MSG = (
    "Remote access is part of a Forager plan. Add it to your account, then turn "
    "on remote access here.")

# The honest error when a server cannot host a tunnel: no host bridge (not a Pi
# appliance) and no in-container WireGuard support.
_TUNNEL_UNSUPPORTED_MSG = (
    "Remote access needs WireGuard support on this server. See the docs. The "
    "container needs the NET_ADMIN capability and the /dev/net/tun device; the "
    "shipped Docker Compose grants both.")

# The port the app itself listens on inside the container (uvicorn binds
# 0.0.0.0:8000; see the Dockerfile CMD). On a server the tunnel runs in this
# container, so the cloud's Caddy route must target this internal port, not the
# host-published 9284 a Pi appliance uses.
SERVER_APP_PORT = 8000


def _tunnel_backend() -> str:
    """Which WireGuard backend brings the tunnel up on this device.

    "bridge": a Pi appliance, where the host bridge (root) owns the interface.
    "local":  a server with in-container WireGuard support (wg tools present and
              /dev/net/tun available), where the app runs wg-quick itself.
    "":       neither, so remote access cannot be hosted here.
    """
    if settings.is_pi_appliance():
        return "bridge"
    if tunnel_local.wg_available():
        return "local"
    return ""


def _tunnel_app_port(backend: str) -> int:
    """The port the cloud's Caddy route should target for this backend: the
    host-published 9284 on a Pi appliance, the app's own internal port on a
    server running WireGuard in-container."""
    return 9284 if backend == "bridge" else SERVER_APP_PORT


async def _tunnel_rollback(base: str, backend: str) -> None:
    """Undo a half-finished enable: take the interface down and tell the cloud
    to disable, both best effort so a rollback never raises. The interface is
    torn down through whichever backend owns it."""
    try:
        if backend == "local":
            await run_in_threadpool(tunnel_local.down)
        else:
            async with bridge_client(timeout=30.0) as c:
                await c.post(f"{_HOST_BRIDGE}/tunnel/down")
    except (httpx.HTTPError, OSError):
        pass
    try:
        async with httpx.AsyncClient(timeout=_CLOUD_TIMEOUT) as c:
            await c.post(f"{base}/v1/tunnel/disable", headers=_cloud_headers())
    except httpx.HTTPError:
        pass


# The honest refusal when remote access is asked for without any second factor
# in place. Enabling a public web address without one would leave the kitchen
# reachable from the internet behind a single password.
_TUNNEL_NEEDS_2FA_MSG = (
    "Turn on two-factor authentication first (Settings, Security), or make sure "
    "your Forager account has it, so your kitchen is not exposed to the internet "
    "without a second factor.")


async def _account_has_2fa() -> bool:
    """Whether the linked Forager account has two-factor sign-in on.

    Reads the account_2fa flag from GET /v1/instance/me. Best effort: an
    unreachable or unlinked cloud returns False, so the caller falls back to
    requiring device 2FA rather than assuming a factor that may not exist."""
    if not settings.cloud_linked():
        return False
    base = settings.cloud_base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=_CLOUD_TIMEOUT) as client:
            r = await client.get(f"{base}/v1/instance/me", headers=_cloud_headers())
    except httpx.HTTPError:
        return False
    if r.status_code != 200:
        return False
    return bool((r.json() or {}).get("account_2fa"))


class TunnelEnablePayload(BaseModel):
    # An optional web address the user chose. Blank keeps the long-standing
    # behavior (the address is derived from the device hostname).
    subdomain: str = ""


@router.get("/tunnel/subdomain-available")
async def tunnel_subdomain_available(name: str = ""):
    """Is a chosen web address free? Proxies the cloud's availability check so
    the browser never talks to Forager directly. Degrades to a plain error the
    Web address field can show, never breaking the settings page.
    """
    if not settings.cloud_linked():
        return {"ok": False, "error":
                "Connect this device to Forager first to choose a web address."}
    base = settings.cloud_base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=_CLOUD_TIMEOUT) as c:
            r = await c.get(f"{base}/v1/tunnel/subdomain-available",
                            headers=_cloud_headers(), params={"name": name})
    except httpx.HTTPError as e:
        return {"ok": False, "error": _safe_error(
            f"Forager could not be reached ({e.__class__.__name__}).",
            settings.cloud_instance_token)}
    if r.status_code != 200:
        return {"ok": False, "error": f"Forager answered with status {r.status_code}."}
    body = r.json() or {}
    return {"ok": True, "available": bool(body.get("available")),
            "sanitized": body.get("sanitized", ""),
            "suggestion": body.get("suggestion", ""),
            "apex": body.get("apex", "")}


@router.post("/tunnel/enable")
async def tunnel_enable(payload: TunnelEnablePayload = TunnelEnablePayload()):
    """Turn on Forager remote access for this device.

    Safety gates come first: the device must be connected to Forager and must
    have a login password set. Then the WireGuard endpoint is set up through
    whichever backend fits this device: the host bridge on a Pi appliance, or
    in-container wg-quick on a server that has WireGuard support. A device with
    neither gets an honest error. Every path keygens, enables on the cloud (with
    the port the cloud's Caddy route should target, and the chosen web address
    when the user picked one), and brings the interface up; any failure rolls
    back and returns a plain-language error.
    """
    if not settings.cloud_linked():
        return {"ok": False, "error":
                "Connect this device to Forager first, then turn on remote access."}
    if not settings.auth_password:
        return {"ok": False, "error":
                "Set a password under Settings, Security first, so your kitchen "
                "is not open to the internet when you turn on remote access."}
    backend = _tunnel_backend()
    if not backend:
        return {"ok": False, "error": _TUNNEL_UNSUPPORTED_MSG}
    # A second factor must be AVAILABLE for outside logins before the kitchen is
    # put on the internet: either the device's own login 2FA, or a linked
    # Forager account that has 2FA (the cloud enforces it on the Forager path).
    if not settings.local_2fa_active() and not await _account_has_2fa():
        return {"ok": False, "needs_2fa": True, "error": _TUNNEL_NEEDS_2FA_MSG}

    base = settings.cloud_base_url.rstrip("/")
    hostname_hint = device_hostname() or APP_NAME

    # 1. Generate the device keypair. The private key stays on the device: on
    #    the bridge (root) for a Pi, in the container's data_dir for a server.
    if backend == "bridge":
        try:
            async with bridge_client(timeout=15.0) as c:
                kr = await c.post(f"{_HOST_BRIDGE}/tunnel/keygen")
        except httpx.HTTPError:
            return {"ok": False, "error":
                    "The device helper could not be reached to set up remote access."}
        if kr.status_code != 200:
            return {"ok": False, "error":
                    "The device could not set up remote access. Try again in a moment."}
        public_key = (kr.json() or {}).get("public_key", "")
    else:
        try:
            public_key = await run_in_threadpool(tunnel_local.keygen)
        except Exception:
            return {"ok": False, "error":
                    "The server could not set up remote access. Try again in a moment."}
    if not public_key:
        return {"ok": False, "error":
                "The device could not set up remote access. Try again in a moment."}

    # 2. Ask the cloud to admit this device (needs a plan that covers it). The
    #    app_port tells the cloud which port its Caddy route should reach: the
    #    host-published 9284 on a Pi, this container's internal port on a server.
    chosen_subdomain = (payload.subdomain or "").strip()
    try:
        async with httpx.AsyncClient(timeout=_CLOUD_TIMEOUT) as c:
            cr = await c.post(
                f"{base}/v1/tunnel/enable", headers=_cloud_headers(),
                json={"public_key": public_key, "hostname_hint": hostname_hint,
                      "subdomain": chosen_subdomain,
                      "app_port": _tunnel_app_port(backend)})
    except httpx.HTTPError as e:
        return {"ok": False, "error": _safe_error(
            f"Forager could not be reached ({e.__class__.__name__}). "
            "Check the internet connection and try again.",
            settings.cloud_instance_token)}
    if cr.status_code == 402:
        return {"ok": False, "needs_plan": True, "error": _TUNNEL_UPGRADE_MSG}
    if cr.status_code == 409:
        # The chosen web address is taken. Nothing was set up on the cloud, so
        # no rollback is needed; hand the free suggestion back to the field.
        detail = {}
        try:
            detail = (cr.json() or {}).get("detail", {})
        except ValueError:
            detail = {}
        detail = detail if isinstance(detail, dict) else {}
        return {"ok": False, "subdomain_taken": True,
                "suggestion": detail.get("suggestion", ""),
                "error": detail.get("message",
                                    "That web address is already taken. Try another one.")}
    if cr.status_code == 503:
        return {"ok": False, "error":
                "Forager remote access is temporarily unavailable. Try again "
                "in a little while."}
    if cr.status_code != 200:
        return {"ok": False, "error":
                f"Forager answered with status {cr.status_code}."}
    data = cr.json() or {}
    public_url = (data.get("public_url") or "").strip().rstrip("/")

    # 3. Bring the interface up with the parameters the cloud handed back.
    if backend == "bridge":
        up_body = {
            "address": data.get("tunnel_ip", ""),
            "server_public_key": data.get("server_public_key", ""),
            "endpoint": data.get("server_endpoint", ""),
            "allowed_ips": data.get("allowed_ips", ""),
            "dns": data.get("dns", ""),
            "keepalive": data.get("keepalive", 25),
        }
        try:
            async with bridge_client(timeout=45.0) as c:
                ur = await c.post(f"{_HOST_BRIDGE}/tunnel/up", json=up_body)
            up_ok = ur.status_code == 200
        except httpx.HTTPError:
            up_ok = False
    else:
        try:
            await run_in_threadpool(
                tunnel_local.up,
                data.get("tunnel_ip", ""), data.get("server_public_key", ""),
                data.get("server_endpoint", ""), data.get("allowed_ips", ""),
                data.get("keepalive", 25))
            up_ok = True
        except Exception:
            up_ok = False
    if not up_ok:
        await _tunnel_rollback(base, backend)
        return {"ok": False, "error":
                "The device could not finish turning on remote access. "
                "It has been switched back off."}

    # 4. Success: adopt the public address so phone links and QR match, and
    #    record that remote access is on.
    to_save = {"tunnel_enabled": True}
    if public_url:
        to_save["qr_public_url"] = public_url
    settings.save(to_save)
    return {"ok": True, "public_url": public_url}


@router.post("/tunnel/disable")
async def tunnel_disable():
    """Turn Forager remote access back off.

    Takes the interface down on the device and tells the cloud to disable,
    both best effort. The public web address is cleared from qr_public_url
    only when it is the tunnel's own address, so a separately configured
    reverse-proxy URL is left alone.
    """
    base = settings.cloud_base_url.rstrip("/")

    # Learn the tunnel's public address before disabling so we know whether the
    # stored QR address is the tunnel's or a user's own.
    tunnel_public = ""
    if settings.cloud_linked():
        try:
            async with httpx.AsyncClient(timeout=_CLOUD_TIMEOUT) as c:
                sr = await c.get(f"{base}/v1/tunnel/status",
                                 headers=_cloud_headers())
            if sr.status_code == 200:
                tunnel_public = (sr.json() or {}).get("public_url", "").strip().rstrip("/")
        except httpx.HTTPError:
            tunnel_public = ""

    # Take the interface down through whichever backend owns it (bridge on a Pi,
    # in-container wg-quick on a server), best effort.
    try:
        if _tunnel_backend() == "local":
            await run_in_threadpool(tunnel_local.down)
        else:
            async with bridge_client(timeout=30.0) as c:
                await c.post(f"{_HOST_BRIDGE}/tunnel/down")
    except (httpx.HTTPError, OSError):
        pass
    if settings.cloud_linked():
        try:
            async with httpx.AsyncClient(timeout=_CLOUD_TIMEOUT) as c:
                await c.post(f"{base}/v1/tunnel/disable", headers=_cloud_headers())
        except httpx.HTTPError:
            pass

    to_save = {"tunnel_enabled": False}
    stored = (settings.qr_public_url or "").strip().rstrip("/")
    if stored and tunnel_public and stored == tunnel_public:
        to_save["qr_public_url"] = ""
    settings.save(to_save)
    return {"ok": True}


@router.get("/tunnel/status")
async def tunnel_status():
    """Remote-access state for the settings card: the device setting merged
    with the cloud's view and the bridge interface's live handshake."""
    base = settings.cloud_base_url.rstrip("/")
    out = {
        "ok": True,
        "enabled": bool(settings.tunnel_enabled),
        "public_url": (settings.qr_public_url or "").rstrip("/") if settings.tunnel_enabled else "",
        "reachable": False,
        "up": False,
        "last_handshake_seconds": None,
    }
    if settings.cloud_linked():
        try:
            async with httpx.AsyncClient(timeout=_CLOUD_TIMEOUT) as c:
                sr = await c.get(f"{base}/v1/tunnel/status",
                                 headers=_cloud_headers())
            if sr.status_code == 200:
                body = sr.json() or {}
                out["reachable"] = True
                out["enabled"] = bool(body.get("enabled", out["enabled"]))
                if body.get("public_url"):
                    out["public_url"] = str(body["public_url"]).rstrip("/")
                if body.get("last_handshake") is not None:
                    out["last_handshake_seconds"] = body.get("last_handshake")
        except httpx.HTTPError:
            out["reachable"] = False
    backend = _tunnel_backend()
    if backend == "bridge":
        try:
            async with bridge_client(timeout=10.0) as c:
                br = await c.get(f"{_HOST_BRIDGE}/tunnel/status")
            if br.status_code == 200:
                bbody = br.json() or {}
                out["up"] = bool(bbody.get("up"))
                if bbody.get("last_handshake_seconds") is not None:
                    out["last_handshake_seconds"] = bbody.get("last_handshake_seconds")
        except httpx.HTTPError:
            pass
    elif backend == "local":
        try:
            local = await run_in_threadpool(tunnel_local.status)
            out["up"] = bool(local.get("up"))
            if local.get("last_handshake_seconds") is not None:
                out["last_handshake_seconds"] = local.get("last_handshake_seconds")
        except OSError:
            pass
    return out


class ScalePayload(BaseModel):
    ui_scale: str = _DEFAULT_UI_SCALE
    display_rotation: int = _DEFAULT_DISPLAY_ROTATION


@router.post("/scale")
async def save_scale(payload: ScalePayload):
    scale = payload.ui_scale if payload.ui_scale in UI_SCALES else _DEFAULT_UI_SCALE
    rot = payload.display_rotation if payload.display_rotation in DISPLAY_ROTATIONS else _DEFAULT_DISPLAY_ROTATION
    settings.save({"ui_scale": scale, "display_rotation": rot})
    return {"ok": True}


class HardwarePresetPayload(BaseModel):
    preset: str = ""


@router.post("/preset/apply")
async def apply_hardware_preset(payload: HardwarePresetPayload):
    """Apply a prebuilt hardware preset in one save (FoodAssistant-kl5n).

    Saves only the settings the preset lists (so nothing else is disturbed),
    then triggers the same downstream effect a manual deck save would: on a Pi
    with a Stream Deck it pushes the deck config so a new rotation or key count
    reaches the running controller. Display scale and rotation are picked up by
    the kiosk on its own, exactly as a manual scale save; the caller refreshes
    the form fields from the returned dict.
    """
    preset = HARDWARE_PRESETS.get(payload.preset)
    if not preset:
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": "Unknown hardware preset."})
    applied = dict(preset.get("settings", {}))
    settings.save(applied)
    # Push the deck config when the preset touches Stream Deck settings and a
    # deck is present, reusing the satellite/appliance push helper rather than
    # duplicating the read-modify-write. Best effort: never fail the apply if
    # the bridge is unreachable or this is not a Pi appliance.
    deck_pushed = False
    if any(k.startswith("streamdeck_") or k == "has_streamdeck" for k in applied):
        try:
            from ..services.satellite import _push_streamdeck_settings
            deck_pushed = await run_in_threadpool(_push_streamdeck_settings)
        except Exception:
            deck_pushed = False
    return {"ok": True, "preset": payload.preset,
            "label": preset.get("label", ""),
            "applied": applied, "deck_pushed": deck_pushed}


@router.post("/save")
async def save_setup(payload: SetupPayload):
    data = payload.model_dump(exclude_unset=True)
    # Changing or removing an existing login password requires the current one,
    # so an unattended or hijacked settings session cannot lock out the owner
    # (FoodAssistant-f403). Setting the first password (none stored yet) needs
    # no current password. A blank auth_password means "keep", so it is exempt.
    if payload.auth_password and settings.auth_password \
            and not verify_secret(payload.current_password, settings.auth_password):
        return {"ok": False,
                "error": "Enter your current password to change or remove it."}
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
    # Deck rotation: only a supported angle persists (FoodAssistant-kl5n).
    if "streamdeck_rotation" in data and data["streamdeck_rotation"] not in DISPLAY_ROTATIONS:
        data["streamdeck_rotation"] = _DEFAULT_DISPLAY_ROTATION
    # 12/24-hour clock reading: only the known values persist.
    if "clock_format" in data and data["clock_format"] not in CLOCK_FORMATS:
        data["clock_format"] = _DEFAULT_CLOCK_FORMAT
    # Shopping-list backend: only the known values persist ("" = automatic).
    if ("shopping_backend" in data
            and data["shopping_backend"] not in ("", "grocy", "mealie")):
        data["shopping_backend"] = ""
    # Screensaver pill size: only the known values persist.
    if ("screensaver_pill_scale" in data
            and data["screensaver_pill_scale"] not in ("normal", "large", "xlarge")):
        data["screensaver_pill_scale"] = "normal"
    # Ken Burns pan/zoom speed: only the known values persist.
    if ("screensaver_ken_burns_speed" in data
            and data["screensaver_ken_burns_speed"] not in ("slow", "normal", "fast")):
        data["screensaver_ken_burns_speed"] = "normal"
    # Screensaver style: an unknown mode falls back to the bouncing logo so a
    # stray value never leaves the panel on a mode the browser cannot draw.
    if ("screensaver_mode" in data
            and data["screensaver_mode"] not in SCREENSAVER_MODES):
        data["screensaver_mode"] = _DEFAULT_SCREENSAVER_MODE
    # Photo source: only the known source names persist (FoodAssistant-af1l).
    if "photo_source" in data:
        from ..services.photo_source import (normalize_photo_source,
                                             invalidate_immich_cache)
        data["photo_source"] = normalize_photo_source(data["photo_source"])
        # A source change must not serve a stale Immich album listing.
        invalidate_immich_cache()
    # Seconds per slideshow photo: keep it in a sane range.
    if "screensaver_photo_seconds" in data:
        try:
            data["screensaver_photo_seconds"] = max(2, min(120, int(data["screensaver_photo_seconds"])))
        except (TypeError, ValueError):
            data["screensaver_photo_seconds"] = 25
    # AI token budget: non-negative integer (0 = no budget).
    if "ai_token_budget" in data:
        try:
            data["ai_token_budget"] = max(0, int(data["ai_token_budget"]))
        except (TypeError, ValueError):
            data.pop("ai_token_budget", None)
    # On-screen Start Page: only 6/15/32 keys are valid (Stream Deck sizes).
    if "start_page_keys" in data and data["start_page_keys"] not in (6, 15, 32):
        data["start_page_keys"] = 15
    # Start Page mode: only the two known modes persist. None means the field was
    # not submitted, so drop it and keep the stored mode.
    if "start_page_mode" in data:
        if data["start_page_mode"] is None:
            data.pop("start_page_mode", None)
        elif data["start_page_mode"] not in ("glance", "custom"):
            data["start_page_mode"] = "glance"
    if data.get("start_page_layout") is None:
        data.pop("start_page_layout", None)  # absent = keep stored layout
    # Merge custom-key definitions built on the Start Page into the shared deck
    # store (Pantry Raider). Custom keys are shared both ways without the Start
    # Page needing the deck's slots:
    #   * update an existing key by id, keeping its Stream Deck slot;
    #   * add a new key unplaced (slot -1);
    #   * drop a key ONLY when the editor that opened it (start_loaded_ids) no
    #     longer lists it, so a key added on the deck since this page loaded is
    #     never clobbered by a stale Start Page save.
    defs = data.pop("start_custom_defs", None)
    loaded_ids = set(data.pop("start_loaded_ids", None) or [])
    if isinstance(defs, list):
        existing = settings.streamdeck_key_overrides or []
        defs_by_id = {d["id"]: d for d in defs
                      if isinstance(d, dict) and d.get("id")}
        result, kept = [], set()
        for o in existing:
            if not isinstance(o, dict) or not o.get("id"):
                continue
            oid = o["id"]
            if oid in defs_by_id:
                entry = dict(defs_by_id[oid]); entry["slot"] = o.get("slot", -1)
                result.append(entry)
            elif oid in loaded_ids:
                continue  # the user removed it on the Start Page
            else:
                result.append(o)  # untouched by this editor; keep as-is
            kept.add(oid)
        for d in defs:
            if isinstance(d, dict) and d.get("id") and d["id"] not in kept:
                entry = dict(d); entry["slot"] = -1; result.append(entry)
        data["streamdeck_key_overrides"] = result
    # Background image (FoodAssistant-e2t6): clamp opacity to 0-100 and only
    # accept an http(s) or the internal serve route as the image URL, so a saved
    # value can never inject a javascript:/data: URL into the CSS background.
    if "background_opacity" in data:
        try:
            data["background_opacity"] = max(0, min(100, int(data["background_opacity"])))
        except (TypeError, ValueError):
            data.pop("background_opacity", None)
    if "background_image_url" in data:
        u = (data["background_image_url"] or "").strip()
        if u and not (u.startswith("http://") or u.startswith("https://")
                      or u.startswith("setup/background/image")):
            data.pop("background_image_url", None)
        else:
            data["background_image_url"] = u
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
    # Drop an unknown nav-visibility value (empty/invalid keeps the stored one).
    if "nav_visibility" in data and data["nav_visibility"] not in NAV_VISIBILITY:
        data.pop("nav_visibility", None)
    # Drop an unknown timer-chips value (empty/invalid keeps the stored one).
    if "timer_chips" in data and data["timer_chips"] not in TIMER_CHIPS:
        data.pop("timer_chips", None)
    # Drop an unknown QR address mode (empty/invalid keeps the stored one).
    if "qr_url_mode" in data and data["qr_url_mode"] not in ("auto", "public"):
        data.pop("qr_url_mode", None)
    # The QR public URL must be a plain http(s) address (or empty to clear it).
    if "qr_public_url" in data:
        u = (data["qr_public_url"] or "").strip()
        if u and not (u.startswith("http://") or u.startswith("https://")):
            data.pop("qr_public_url", None)
        else:
            data["qr_public_url"] = u.rstrip("/")
    # Timezone: "" (auto/system) or a valid IANA name; drop anything else so a
    # typo never breaks timestamp rendering.
    if "timezone" in data and data["timezone"]:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(data["timezone"])
        except Exception:
            data.pop("timezone", None)
    # Scheduled reboot time: "" (off) or 24h HH:MM.
    if "scheduled_reboot_time" in data and data["scheduled_reboot_time"]:
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", str(data["scheduled_reboot_time"])):
            data.pop("scheduled_reboot_time", None)
    # Reboot frequency: "" (legacy, a set time means nightly), off, nightly,
    # or weekly; the day is 0=Sunday .. 6=Saturday. Anything else keeps the
    # stored value (FoodAssistant-8x4u).
    if "scheduled_reboot_frequency" in data and (
            data["scheduled_reboot_frequency"] not in ("", "off", "nightly", "weekly")):
        data.pop("scheduled_reboot_frequency", None)
    if "scheduled_reboot_day" in data:
        try:
            day = int(data["scheduled_reboot_day"])
        except (TypeError, ValueError):
            day = -1
        if 0 <= day <= 6:
            data["scheduled_reboot_day"] = day
        else:
            data.pop("scheduled_reboot_day", None)
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
    # Custom nav tabs (FoodAssistant-9gdz): normalize to {id,label,icon,url,
    # parent} and drop invalid entries so a malformed POST never persists. An
    # absent field leaves the stored tabs alone; an empty list clears them.
    if "custom_nav_tabs" in data:
        if data["custom_nav_tabs"] is None:
            data.pop("custom_nav_tabs", None)
        else:
            data["custom_nav_tabs"] = _clean_custom_nav_tabs(data["custom_nav_tabs"])
    # Built-in nesting map: keep only string->string pairs for known tab keys.
    if "nav_parents" in data:
        if data["nav_parents"] is None:
            data.pop("nav_parents", None)
        else:
            data["nav_parents"] = _clean_nav_parents(data["nav_parents"])
    # Drop an unknown deployment mode rather than persisting a broken value;
    # an empty/absent mode leaves the existing choice untouched.
    if data.get("deployment_mode") and data["deployment_mode"] not in DEPLOYMENT_MODES:
        data.pop("deployment_mode", None)
    # Same for an unknown wake-on-motion mode: keep the stored value.
    if "wake_on_motion" in data and data["wake_on_motion"] not in ("auto", "on", "off"):
        data.pop("wake_on_motion", None)
    # And an unknown wake-on-presence mode.
    if "wake_on_presence" in data and data["wake_on_presence"] not in ("auto", "on", "off"):
        data.pop("wake_on_presence", None)
    # And the update channel: only "stable" (releases) or "main" (every change).
    if "update_channel" in data and data["update_channel"] not in ("stable", "main"):
        data.pop("update_channel", None)
    if data.get("remote_server_url"):
        data["remote_server_url"] = data["remote_server_url"].rstrip("/")
    # The backup remote later becomes a positional rclone argument, so only a
    # safe remote:path (or absolute path) shape is ever stored; anything else,
    # in particular a value starting with "-", is dropped so it can never
    # smuggle a flag into the rclone command (see services/backup_remote.py).
    if "rclone_remote" in data:
        from ..services.backup_remote import valid_remote
        v = (data["rclone_remote"] or "").strip()
        if v and not valid_remote(v):
            data.pop("rclone_remote", None)
        else:
            data["rclone_remote"] = v   # "" clears the remote
    # A Reolink camera's login lives on the server and is never rendered into the
    # page, so a plain "Save cameras" (which rebuilds the list from the visible
    # rows) posts the entry without its password. Restore the stored password by
    # matching the camera's identity, the same "blank keeps the secret" rule the
    # HA token uses, so re-saving the list does not wipe the credentials.
    if "streamdeck_cameras" in data and isinstance(data["streamdeck_cameras"], list):
        data["streamdeck_cameras"] = _merge_reolink_secrets(data["streamdeck_cameras"])
    settings.save(data)
    reset_providers()   # apply new provider/model/key without a restart
    from ..services.mealie import reset_cache as reset_mealie_cache, reset_staple_cache
    reset_mealie_cache()
    reset_staple_cache()
    # Mirror the kiosk display idle timeout to the host bridge, which owns the
    # blanking loop (FoodAssistant-otiy). Best-effort and Pi-only.
    if "display_idle_timeout" in data or "wake_on_motion" in data or "wake_on_presence" in data:
        await _push_display_idle()
    # Auto-provision the touch overlay when the display type is (re)chosen, so an
    # ADS7846 SPI panel gets SPI + its overlay written without a separate button
    # press (FoodAssistant-vbfp). Best-effort and Pi-only; a reboot loads it.
    resp = {"ok": True}
    if data.get("display_type") and is_raspberry_pi():
        provisioned = await _provision_touch_for_display(data["display_type"])
        if provisioned and provisioned.get("needs_reboot"):
            resp["touch_needs_reboot"] = True
    # Mirror the timezone and nightly-reboot schedule to the host (Pi appliance),
    # so the system clock and the reboot timer match the saved settings. Best
    # effort: a missing/old bridge just leaves the host as-is.
    if settings.is_pi_appliance():
        # Mirror the update channel to the host as well: the OTA helper runs on
        # the host and reads /etc/foodassistant/update-channel, which the app
        # container cannot write itself, so the bridge lands it there (wkwx).
        # Best effort; the update trigger re-pushes it so a stale bridge only
        # delays the switch by one update.
        if "update_channel" in data:
            await _push_update_channel(settings.update_channel)
        if data.get("timezone"):
            try:
                async with bridge_client(timeout=15.0) as c:
                    await c.post(f"{_HOST_BRIDGE}/system/timezone",
                                 json={"tz": data["timezone"]})
            except Exception:
                pass
        if any(k in data for k in ("scheduled_reboot_time",
                                   "scheduled_reboot_frequency",
                                   "scheduled_reboot_day")):
            # Push the effective schedule from the merged settings, so a save
            # of just the frequency (or just the day) still lands the whole
            # picture on the host. An empty frequency keeps the legacy rule:
            # a set time means nightly (FoodAssistant-8x4u).
            freq = settings.scheduled_reboot_frequency or (
                "nightly" if settings.scheduled_reboot_time else "off")
            try:
                async with bridge_client(timeout=15.0) as c:
                    await c.post(f"{_HOST_BRIDGE}/system/scheduled-reboot",
                                 json={"time": "" if freq == "off" else settings.scheduled_reboot_time,
                                       "frequency": freq,
                                       "day": settings.scheduled_reboot_day})
            except Exception:
                pass
    return resp


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


class FirstRunPayload(BaseModel):
    base_url: str = ""


class FirstRunRevealPayload(BaseModel):
    service: str = ""


@router.post("/first-run/reveal")
async def first_run_reveal(payload: FirstRunRevealPayload):
    """Reveal the backend sign-in that first-run provisioning generated.

    Only answers for a password provisioning actually stored, and only on an
    explicit request from the settings pane; the secret is never rendered
    into the page itself. Registered before the /first-run/{service} route
    so "reveal" is never taken as a service name.
    """
    if payload.service == "grocy" and settings.grocy_admin_password:
        return {"ok": True, "username": "admin",
                "password": settings.grocy_admin_password}
    if payload.service == "mealie" and settings.mealie_admin_password:
        from ..services.first_run import MEALIE_DEFAULT_EMAIL
        return {"ok": True, "username": MEALIE_DEFAULT_EMAIL,
                "password": settings.mealie_admin_password}
    return JSONResponse({"ok": False, "error": "No saved sign-in for that service."})


@router.post("/first-run/{service}")
async def first_run_provision(service: str, payload: FirstRunPayload | None = None):
    """Set up Grocy or Mealie automatically ("Set up for me").

    Runs the first-run provisioning engine once against the given (or
    configured/suggested) address. Safe on any install: an already-connected
    backend, or one whose sign-in is no longer the factory default, is left
    untouched and reported as such. A satellite's backends live on its main
    server, so this is not offered there.
    """
    if settings.is_satellite():
        return JSONResponse({"ok": False, "error": "Backends are managed on the main server."})
    from ..services import first_run
    base = (payload.base_url if payload else "") or ""
    if service == "grocy":
        return await first_run.provision_grocy(base)
    if service == "mealie":
        return await first_run.provision_mealie(base)
    return JSONResponse({"ok": False, "error": "Unknown service."}, status_code=404)


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

    if p == "cloud":
        # Forager: "connected" means the stored instance token is
        # accepted by GET /v1/instance/me. No key or model to test.
        if not settings.cloud_linked():
            return {"ok": False, "error": ("Not linked to Forager "
                                           "yet. Enter a pairing code in the "
                                           "Forager card first.")}
        base = settings.cloud_base_url.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=_CLOUD_TIMEOUT) as client:
                r = await client.get(f"{base}/v1/instance/me",
                                     headers=_cloud_headers())
            if r.status_code == 200:
                ent = (r.json() or {}).get("entitlement", {})
                if ent.get("active"):
                    return {"ok": True, "message": "Connected: subscription active."}
                return {"ok": True, "message": ("Connected, but the account has "
                                                "no active subscription.")}
            if r.status_code == 401:
                return {"ok": False, "error": ("The cloud no longer accepts this "
                                               "install's link. Unlink and pair "
                                               "again with a new code.")}
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"ok": False, "error": _safe_error(e, settings.cloud_instance_token)}

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


# Local two-factor authentication for the app's own login (FoodAssistant-x1ty).
# The device's own authenticator-app 2FA for the LOCAL password: generate a
# secret and QR, confirm a code to turn it on (revealing recovery codes once),
# and turn it back off (or regenerate the codes) after proving a current code
# or the app password.

from .. import totp as local_totp


class TOTPVerifyPayload(BaseModel):
    secret: str
    code: str


class TOTPCredentialPayload(BaseModel):
    # A live authenticator code, one of the recovery codes, or the app password:
    # any one proves the owner is present before a 2FA change.
    credential: str = ""


def _totp_account_label() -> str:
    """The label an authenticator app shows for this device's entry: the linked
    Forager account when available, otherwise the device hostname or the app
    name, so several kitchens are told apart in the app's list."""
    if settings.cloud_linked():
        # Best effort: the account email is only known after a cloud round-trip,
        # so fall back to the hostname rather than dial out on setup.
        return device_hostname() or APP_NAME
    return device_hostname() or APP_NAME


def _local_2fa_credential_ok(credential: str) -> bool:
    """Whether ``credential`` proves the owner may change 2FA: a live TOTP code,
    an unused recovery code, or the app (admin) password."""
    credential = (credential or "").strip()
    if not credential:
        return False
    secret = settings.local_totp_effective_secret()
    if secret and local_totp.totp_verify(secret, credential):
        return True
    matched, _remaining = local_totp.consume_recovery_code(
        credential, settings.local_totp_recovery)
    if matched:
        return True
    if settings.auth_password and verify_secret(credential, settings.auth_password):
        return True
    return False


@router.post("/totp/generate")
async def totp_generate():
    """Generate a fresh TOTP secret and its QR for enrollment. Nothing is saved
    until the user confirms a code at /totp/verify."""
    secret = local_totp.generate_totp_secret()
    uri = local_totp.otpauth_uri(secret, _totp_account_label(), APP_NAME)
    return {"secret": secret, "qr": local_totp.qr_data_uri(uri), "uri": uri}


@router.post("/totp/verify")
async def totp_verify(payload: TOTPVerifyPayload):
    """Confirm the authenticator app is in sync, then turn on 2FA and mint the
    one-time recovery codes (shown here once, stored only as hashes)."""
    if not local_totp.totp_verify(payload.secret, payload.code):
        return {"ok": False,
                "error": "Code did not match. Check your authenticator app clock."}
    codes = local_totp.generate_recovery_codes()
    settings.save({
        "local_totp_secret": payload.secret,
        "local_totp_enabled": True,
        "local_totp_recovery": local_totp.hash_recovery_codes(codes),
        # Clear any legacy secret so there is one source of truth going forward.
        "totp_secret": "",
    })
    return {"ok": True, "recovery_codes": codes,
            "message": "Two-factor authentication is on. Save your recovery codes."}


@router.post("/totp/disable")
async def totp_disable(payload: TOTPCredentialPayload = TOTPCredentialPayload()):
    """Turn 2FA off after the owner proves a current code or the app password,
    so a walk-up at an open settings page cannot silently remove it."""
    if settings.local_2fa_active() and not _local_2fa_credential_ok(payload.credential):
        return {"ok": False,
                "error": "Enter a current code or your password to turn off two-factor authentication."}
    settings.save({"local_totp_secret": "", "local_totp_enabled": False,
                   "local_totp_recovery": [], "totp_secret": ""})
    return {"ok": True, "message": "Two-factor authentication is off."}


@router.post("/totp/recovery")
async def totp_recovery(payload: TOTPCredentialPayload = TOTPCredentialPayload()):
    """Mint a fresh set of recovery codes (the old set stops working), after the
    owner proves a current code or the app password."""
    if not settings.local_2fa_active():
        return {"ok": False,
                "error": "Turn on two-factor authentication first."}
    if not _local_2fa_credential_ok(payload.credential):
        return {"ok": False,
                "error": "Enter a current code or your password to make new recovery codes."}
    codes = local_totp.generate_recovery_codes()
    settings.save({"local_totp_recovery": local_totp.hash_recovery_codes(codes)})
    return {"ok": True, "recovery_codes": codes,
            "message": "New recovery codes. The old ones no longer work."}


# Pi host bridge endpoints
# -------------------------
# These call a small helper service running on 127.0.0.1:9299 on the Pi host.
# Because docker-compose.appliance.yml uses network_mode: host, localhost in the
# container is the same as localhost on the host, so no special networking is needed.
# On non-Pi or non-appliance installs the endpoints return a clear error.

_HOST_BRIDGE = "http://127.0.0.1:9299"


async def _push_display_idle() -> bool:
    """Push the display idle timeout, wake-on-motion, and wake-on-presence
    modes to the host bridge (Pi only, best-effort).

    The bridge owns the kiosk display blanking loop, the accelerometer motion
    poll, and the LD2410C presence poll, and persists all three values
    (FoodAssistant-otiy, fr5, 6z8c). An older bridge simply ignores fields it
    does not recognize."""
    if not is_raspberry_pi():
        return False
    try:
        async with bridge_client(timeout=4.0) as c:
            r = await c.post(
                f"{_HOST_BRIDGE}/display/idle",
                json={
                    "minutes": settings.display_idle_timeout,
                    "wake_on_motion": settings.wake_on_motion,
                    "wake_on_presence": settings.wake_on_presence,
                },
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
        async with bridge_client(timeout=4.0) as c:
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
        async with bridge_client(timeout=4.0) as c:
            r = await c.get(f"{_HOST_BRIDGE}/activity")
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/kiosk/screensaver")
async def kiosk_screensaver(request: Request):
    """Relay the kiosk screensaver overlay's show/hide state to the host bridge
    so the Stream Deck can raise its display-off logo during a soft sleep, where
    the panel stays lit but the saver covers it (FoodAssistant-qh8p).

    The screensaver reports this edge-triggered on show and hide only, and the
    bridge stores it without touching the shared activity epoch, so it is never
    a wake source (FoodAssistant-ofip). No-op off a Pi."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    active = bool(body.get("active")) if isinstance(body, dict) else False
    if not is_raspberry_pi():
        return {"ok": True, "screensaver_active": active}
    try:
        async with bridge_client(timeout=4.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/screensaver", json={"active": active})
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/display/blank")
async def display_blank():
    """Manually blank the kiosk display (Pi only)."""
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    try:
        async with bridge_client(timeout=6.0) as c:
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
        async with bridge_client(timeout=6.0) as c:
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
        async with bridge_client(timeout=4.0) as c:
            wifi = (await c.get(f"{_HOST_BRIDGE}/wifi/status")).json()
            hn = (await c.get(f"{_HOST_BRIDGE}/hostname")).json()
        # Which link actually carries the network (FoodAssistant-1idf). The
        # bridge reports the active connection from the default route; classify
        # it here (a pure, tested helper) rather than in the bridge so a Pi on
        # Ethernet shows a calm "not in use" Wi-Fi line instead of a scary
        # "unavailable". Fall back to the bridge's own value if present.
        active = classify_active_connection(wifi.get("default_route", ""))
        if active == "none":
            active = wifi.get("active_connection", "")
        return {
            "ok": True,
            "ssid": wifi.get("ssid", ""),
            "wifi_state": wifi.get("state", ""),
            "wifi_detail": wifi.get("detail", ""),
            "ethernet": wifi.get("ethernet", {}),
            "active_connection": active,
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
        async with bridge_client(timeout=25.0) as c:
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
        async with bridge_client(timeout=35.0) as c:
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
        async with bridge_client(timeout=10.0) as c:
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
        async with bridge_client(timeout=4.0) as c:
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
        async with bridge_client(timeout=20.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/display/rotation",
                             json={"degrees": payload.degrees, "reboot": payload.reboot})
        settings.save({"display_rotation": payload.degrees})
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


# Map a wizard display type to the touch driver whose boot config the host
# bridge applies. Only ADS7846 SPI panels need a config.txt overlay written from
# the running system; USB/DSI/generic need none (DSI's panel overlay is handled
# by firstboot, and its touch is I2C which libinput picks up on its own).
_DISPLAY_TOUCH_DRIVER = {
    "ads7846_hdmi": "ads7846",
    "waveshare_hdmi": "usb",
    "dsi_7inch": "generic",
    "generic": "generic",
}


async def _provision_touch_for_display(display_type: str) -> dict | None:
    """Best-effort: write the touch overlay a display type needs (Pi only).

    Called when the display type is saved so an ADS7846 SPI panel is set up
    without a separate button press. Only ADS7846 needs a boot-config change;
    other types are skipped. Never raises. Returns the bridge result, or None."""
    driver = _DISPLAY_TOUCH_DRIVER.get(display_type, "generic")
    if driver != "ads7846":
        return None
    try:
        async with bridge_client(timeout=15.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/touch/provision",
                             json={"driver": driver, "reboot": False})
        return r.json()
    except Exception:
        return None


@router.post("/touch/provision")
async def touch_provision(request: Request):
    """Apply the boot config the attached touch panel needs (Pi appliance only).

    Fills the gap where a display type chosen in the wizard after first boot was
    never provisioned: an ADS7846 SPI panel needs SPI enabled and the ads7846
    overlay in config.txt before its touch registers. Uses the saved display
    type unless one is passed in the body. A reboot is required to load a new
    overlay."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    try:
        body = await request.json()
    except Exception:
        body = {}
    dtype = (body.get("display_type") or settings.display_type or "generic")
    reboot = bool(body.get("reboot", False))
    driver = _DISPLAY_TOUCH_DRIVER.get(dtype, "generic")
    try:
        async with bridge_client(timeout=20.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/touch/provision",
                             json={"driver": driver, "reboot": reboot})
        # An old host bridge (updated app, stale bridge) has no such route and
        # answers 404 {"error": "not found"}. Say so plainly instead of leaking
        # a bare "not found" (FoodAssistant-vbfp): the bridge is redeployed by
        # the updater now, so the fix is to run the update once more or reboot.
        if r.status_code == 404:
            return JSONResponse({"ok": False, "error":
                "The host bridge on this device is out of date and cannot set up "
                "touch yet. Run Backup & Updates, Update once more (it now "
                "refreshes the bridge), or reboot, then try again."})
        out = r.json()
        out["driver"] = driver
        return out
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/maintenance/reboot")
async def maintenance_reboot():
    """Reboot the appliance now via the host bridge (Pi appliance only)."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    try:
        async with bridge_client(timeout=15.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/reboot")
        if r.status_code != 200:
            # An out-of-date bridge answers 404 {"error": "not found"} for a
            # route it predates; surface something actionable instead of the
            # bare bridge body (FoodAssistant-pnz4).
            return JSONResponse({"ok": False, "error":
                "The device helper does not support this yet; run Update, then try again."})
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/maintenance/reload")
async def maintenance_reload():
    """Re-read settings.json and reset provider/recipe caches without a restart.

    Works on any platform: it picks up an out-of-band settings change (a restore
    or hand edit) and rebuilds the cached AI provider and Mealie clients so the
    new values take effect immediately (FoodAssistant-wvwm)."""
    applied = settings.reload()
    reset_providers()
    from ..services.mealie import reset_cache as reset_mealie_cache, reset_staple_cache
    reset_mealie_cache()
    reset_staple_cache()
    return {"ok": True, "reloaded": len(applied)}


@router.post("/streamdeck/restart")
async def streamdeck_restart():
    """Restart the Stream Deck systemd service (Pi appliance only)."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    try:
        async with bridge_client(timeout=15.0) as c:
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
        async with bridge_client(timeout=35.0) as c:
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
        async with bridge_client(timeout=10.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/mealie/start")
        result = r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
    # Zero-touch first run (FoodAssistant-syxf): once the just-started Mealie
    # serves HTTP, connect it automatically (API token, secured sign-in,
    # default shopping list). Background task with patient polling, since the
    # first start downloads the image; it stops on its own if Mealie turns out
    # to be configured already. On the appliance the app reaches Mealie on
    # loopback at the published port.
    if not (settings.mealie_api_key and settings.mealie_base_url):
        from ..services.first_run import provision_mealie_when_up
        asyncio.create_task(provision_mealie_when_up("http://localhost:9285"))
    return result


@router.get("/mealie/status")
async def mealie_status():
    """Mealie start progress (not-installed / starting / running), via the bridge."""
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    try:
        async with bridge_client(timeout=12.0) as c:
            r = (await c.get(f"{_HOST_BRIDGE}/mealie/status")).json()
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)}


_LOG_NAMES = {"mealie", "kiosk", "streamdeck", "grocy"}


@router.get("/grocy/local-status")
async def grocy_local_status():
    """Whether the appliance's local Grocy answers HTTP yet (Pi appliance only).

    The setup wizard polls this on a pi_hosted device while the first-boot
    Grocy/stack install is still running, so it can show the live install log
    until Grocy starts serving (FoodAssistant-n5ky). Returns {ok, serving}.
    """
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    return {"ok": True, "serving": bool(await _detect_local_grocy())}


@router.get("/logs/{name}")
async def install_logs(name: str):
    """Tail of an install/start log (mealie / kiosk / streamdeck / grocy), via
    the bridge.

    Mirrors the /mealie/status proxy: the setup UI polls this while a start or
    install is in flight to show live output. Returns {ok, name, running,
    lines}. Unknown names and bridge errors are reported, never raised.
    """
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    if name not in _LOG_NAMES:
        return {"ok": False, "error": "unknown log name"}
    try:
        async with bridge_client(timeout=6.0) as c:
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
        async with bridge_client(timeout=6.0) as c:
            r = (await c.get(f"{_HOST_BRIDGE}/hardware/status")).json()
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)}


# Sync the system-warning action items at most this often. The navbar polls
# this route from every open page (5-minute interval plus one hit per page
# load), so the throttle keeps the inbox sync to once a minute, matching the
# bridge's own monitoring loop.
_WARN_SYNC_THROTTLE_SECS = 60.0
_warn_last_sync = 0.0


def _sync_warning_items(warnings: list) -> None:
    """Raise/retire system-warning action items from the bridge feed, throttled.

    Best-effort by design: the health poll must keep answering even when the
    database is momentarily busy, so any failure here is swallowed.
    """
    global _warn_last_sync
    # A satellite's inbox lives on the main server (the action-items routes
    # forward upstream), so locally raised items would never be seen there;
    # the navbar icon and the settings banner still show its warnings.
    if settings.is_satellite():
        return
    import time as _time
    now = _time.monotonic()
    if now - _warn_last_sync < _WARN_SYNC_THROTTLE_SECS:
        return
    _warn_last_sync = now
    from ..services import action_items
    db = SessionLocal()
    try:
        action_items.sync_system_warnings(db, warnings)
    except Exception:
        pass
    finally:
        db.close()


@router.get("/system/health")
async def system_health():
    """Pi power/thermal/disk warnings, via the host bridge.

    Served from the bridge's continuously monitored /system/warnings snapshot
    (60-second loop), falling back to the on-demand /system/health probe on an
    older bridge. Off a Pi there is no bridge to probe, so return a clean "no
    warnings" shape (rather than an error) so the navbar indicator simply shows
    nothing instead of a failure on a server or phone. As a side effect the
    active warnings are mirrored into the action-items inbox (throttled), so a
    condition like undervoltage shows up there once and archives itself when
    it clears.
    """
    if not is_raspberry_pi():
        return {"ok": True, "warnings": []}
    try:
        async with bridge_client(timeout=6.0) as c:
            resp = await c.get(f"{_HOST_BRIDGE}/system/warnings")
            if resp.status_code == 404:  # older bridge: on-demand probe only
                resp = await c.get(f"{_HOST_BRIDGE}/system/health")
            r = resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e), "warnings": []}
    if isinstance(r, dict):
        _sync_warning_items(r.get("warnings") or [])
    return r


@router.get("/resources")
async def system_resources():
    """Live hardware readings for the Settings Resources pane.

    CPU use (two /proc/stat samples a moment apart), load, memory, disks,
    uptime, and temperature come from the pure collector in
    services/resources.py, which omits anything this environment cannot
    provide. On a Pi the host bridge's warnings snapshot adds the firmware
    power/throttle state (and a temperature fallback when the container
    cannot see a thermal zone); an unreachable bridge just leaves those out.
    """
    from ..services import resources as res
    from ..services import beszel
    prev = await run_in_threadpool(res.sample_cpu)
    await asyncio.sleep(0.3)
    cur = await run_in_threadpool(res.sample_cpu)
    data = await run_in_threadpool(res.collect, settings.data_dir, prev, cur)
    link = beszel.dashboard_link(settings.beszel_enabled, settings.beszel_url)
    if link:
        data["beszel_url"] = link
    if is_raspberry_pi():
        try:
            async with bridge_client(timeout=4.0) as c:
                r = (await c.get(f"{_HOST_BRIDGE}/system/warnings")).json()
            if isinstance(r, dict):
                power = res.decode_throttle_word(r.get("throttled"))
                if power:
                    data["power"] = power
                if "temperature" not in data and isinstance(r.get("temp_c"), (int, float)):
                    data["temperature"] = {"celsius": round(float(r["temp_c"]), 1)}
        except Exception:
            pass
    data["ok"] = True
    return data


async def _safe(coro, default):
    """Await a check coroutine, degrading any failure to a default (never raises).

    The Status dashboard aggregates several subsystem checks; a single slow or
    broken one must not fail the whole page, so every probe is wrapped here and
    a failure just shows that row as unknown/unreachable.
    """
    try:
        return await coro
    except Exception:
        return default


async def _ha_probe() -> dict:
    """Lightweight Home Assistant reachability check for the Status dashboard.

    Returns ``{configured, ok}``: configured is whether an HA address and token
    are saved; ok is whether HA answered its API root. Credentials stay on the
    server (only the boolean result leaves). Short timeout, fails soft.
    """
    base = (settings.streamdeck_ha_base_url or "").strip().rstrip("/")
    token = (settings.streamdeck_ha_token or "").strip()
    if not base or not token:
        return {"configured": False, "ok": False}
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            r = await c.get(f"{base}/api/", headers={"Authorization": f"Bearer {token}"})
        return {"configured": True, "ok": r.status_code == 200}
    except Exception:
        return {"configured": True, "ok": False}


@router.get("/status/summary")
async def status_summary_route():
    """Aggregate every subsystem's health for the Settings Status dashboard.

    Reuses the same checks that back the individual panes (network, Forager,
    the cached update result, Grocy/Mealie/Home Assistant connections), gathers
    only the rows this deployment mode shows, and runs them concurrently with
    short timeouts. Each probe degrades to unknown/unreachable on any error, so
    the dashboard always renders. The raw results are handed to the pure
    ``status_summary.build_summary`` mapper, which decides each pill's state and
    detail; this route only does the IO.
    """
    is_pi = is_raspberry_pi()
    sat = settings.is_satellite()

    # Which rows this mode shows, and the coroutine (or value) that feeds each.
    # Kept in step with the Jinja gating in _pane_status.html.
    probes: dict = {}

    if is_pi:
        probes["connection"] = _safe(network_status(), {"ok": False})
    if is_pi and not sat:  # pi_hosted: a physical Pi we own end to end
        probes["pi_health"] = _safe(system_health(), {"ok": False, "warnings": []})

    # Software update: read the cached update-check bookkeeping, never a live
    # GitHub call, so the dashboard is instant.
    probes["update"] = {
        "checked_at": settings.update_last_checked,
        "available": settings.update_last_available,
        "latest": settings.update_last_latest,
    }

    if not sat:
        probes["forager"] = _safe(cloud_status(), None)
        probes["remote_access"] = _safe(tunnel_status(), None)
        grocy_configured = bool(
            settings.grocy_api_key and settings.grocy_base_url
            and settings.grocy_base_url != "http://grocy:80")
        if grocy_configured:
            probes["grocy"] = _safe(_grocy_status_probe(), {"configured": True, "ok": False})
        else:
            probes["grocy"] = {"configured": False, "ok": False}
        if settings.mealie_api_key and settings.mealie_base_url:
            probes["mealie"] = _safe(_mealie_status_probe(), {"configured": True, "ok": False})
    else:
        probes["main_server"] = settings.satellite_last_sync if isinstance(
            settings.satellite_last_sync, dict) else {}

    if settings.streamdeck_ha_base_url and settings.streamdeck_ha_token:
        probes["home_assistant"] = _safe(_ha_probe(), {"configured": True, "ok": False})

    # Resolve the coroutines concurrently, then map to pill states (pure).
    keys = list(probes.keys())
    resolved = await asyncio.gather(*[
        v if asyncio.iscoroutine(v) else _as_value(v) for v in probes.values()
    ])
    raw = dict(zip(keys, resolved))
    return status_summary.build_summary(raw)


async def _as_value(v):
    """Wrap a plain value so it can sit beside coroutines in asyncio.gather."""
    return v


async def _grocy_status_probe() -> dict:
    """Grocy reachability for the dashboard, reusing the pane's test path."""
    res = await test_grocy(TestGrocyPayload())
    if isinstance(res, JSONResponse):
        res = json.loads(res.body)
    return {"configured": True, "ok": bool(res.get("ok"))}


async def _mealie_status_probe() -> dict:
    """Mealie reachability for the dashboard, reusing the pane's test path."""
    res = await test_mealie(TestMealiePayload())
    if isinstance(res, JSONResponse):
        res = json.loads(res.body)
    return {"configured": True, "ok": bool(res.get("ok"))}


@router.post("/kiosk/install")
async def kiosk_install():
    """Provision the kiosk service for a display attached after first install."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    try:
        async with bridge_client(timeout=15.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/kiosk/install")
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/update")
async def update_software():
    """Pull the latest version and restart the service, via the host bridge.

    Available on any Pi appliance (both Pi Remote and Pi Hosted) since both run
    the host bridge. The bridge shells out to foodassistant-update, which adapts
    to the device: a Pi Remote runs from a Python venv (git pull, venv pip when
    requirements changed, service restart), while a Pi Hosted box runs from a
    Docker image (docker compose pull + recreate the service container, with a
    build-from-source fallback). Either way the bridge runs the work
    synchronously and returns {ok, before, after, restarted, log}; a failure
    leaves the running version untouched and reports the error. The work can
    take a couple of minutes on a Pi, so the proxy timeout is generous.
    """
    if not settings.is_pi_appliance():
        return JSONResponse(
            {"ok": False, "error": "In-app updates are only available on Pi appliances."})
    return JSONResponse(await run_host_bridge_update())


async def _push_update_channel(channel: str) -> bool:
    """Land the update channel in /etc/foodassistant/update-channel via the host
    bridge, where the OTA helper reads it (FoodAssistant-wkwx). Best effort: an
    old bridge without the route answers 404 and the channel just stays as-is
    until the next update refreshes the bridge. Never raises."""
    try:
        # bridge_client sends the X-Bridge-Token; this is a state-changing POST,
        # so it must carry the token once the bridge enforces (FoodAssistant-ow4f).
        async with bridge_client(timeout=10.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/update/channel",
                             json={"channel": channel})
        return r.status_code == 200
    except Exception:
        return False


async def run_host_bridge_update() -> dict:
    """POST the host bridge OTA and return its JSON result. Shared by the manual
    Update button and the automatic-update scheduler (FoodAssistant-k2kk).

    The current update channel is re-pushed first so the helper always follows
    the saved setting, even when the settings-save push was missed (bridge down
    or predating the /update/channel route)."""
    await _push_update_channel(settings.update_channel)
    try:
        async with bridge_client(timeout=620.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/update")
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/update-server")
async def update_server():
    """Apply an update now on a non-Pi server by triggering Watchtower's HTTP
    API, so the user does not wait for the daily poll. The watchtower service in
    docker-compose.prod.yml exposes the API with a shared token; the app reaches
    it on the compose network. Degrades gracefully when watchtower is not
    running or not configured.
    """
    if settings.is_pi_appliance():
        return JSONResponse(
            {"ok": False, "error": "On a Pi appliance use the Update now button above."})
    url = os.environ.get("WATCHTOWER_URL", "http://watchtower:8080").rstrip("/")
    token = os.environ.get("WATCHTOWER_HTTP_API_TOKEN", "")
    if not token:
        return JSONResponse({"ok": False, "error": (
            "Automatic updater not configured. Start the watchtower service "
            "(it is in the production compose) or run the commands below.")})
    try:
        # Fail fast on connect so a missing/stopped watchtower returns a clear
        # message in a couple of seconds instead of hanging until a reverse proxy
        # cuts the request (which surfaced as a generic "could not reach the
        # server"). Watchtower itself replies to /v1/update quickly.
        timeout = httpx.Timeout(connect=4.0, read=30.0, write=10.0, pool=4.0)
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(f"{url}/v1/update",
                             headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 200:
            return {"ok": True, "message": (
                "Update triggered. If a newer image was published the app will "
                "pull it and restart in a moment.")}
        return JSONResponse(
            {"ok": False, "error": f"Updater returned HTTP {r.status_code}."})
    except Exception as e:
        return JSONResponse({"ok": False, "error": (
            "Could not reach the Watchtower updater, so it is probably not "
            "running on this server. Add the watchtower service from the current "
            "docker-compose.prod.yml and run docker compose up -d, or use the "
            f"commands below. ({e.__class__.__name__})")})


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
        async with bridge_client(timeout=920.0) as c:
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
            async with bridge_client(timeout=3.0) as c:
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
                # 12/24-hour clock reading, so the deck's clock key matches the
                # kiosk and app pages instead of always rendering 24-hour (ylax).
                payload["config"]["clock_format"] = settings.clock_format
                # The idle timeout and the display-off logo choice live in app
                # settings; stamp them so they actually reach the controller.
                # The timeout was saved but never written to config.toml before
                # (FoodAssistant-3fdq), which is why the deck never blanked.
                payload["config"]["idle_timeout_minutes"] = int(
                    settings.streamdeck_idle_timeout or 0)
                payload["config"]["logo_when_display_off"] = bool(
                    settings.streamdeck_logo_when_display_off)
                # Deck rotation is an app setting too (so a hardware preset can
                # set it, FoodAssistant-kl5n), but it is deliberately NOT stamped
                # here: the deck editor already posts the rotation it wants, and
                # other savers of this config (the camera pane) do a read-modify-
                # write that must not have their rotation clobbered by a settings
                # value that has not migrated yet. The preset reaches the deck
                # through the push helper, which overlays the setting directly.
                # Home Assistant credentials, key map, and cameras live in app
                # settings (one source of truth, server or Pi). Stamp them so the
                # deck always gets the server's values regardless of what the page
                # posted, and a satellite's deck inherits them (FoodAssistant-cr50).
                payload["config"]["ha_base_url"] = settings.streamdeck_ha_base_url
                payload["config"]["ha_token"] = settings.streamdeck_ha_token
                payload["config"]["ha_slots"] = settings.streamdeck_ha_slots
                from ..services import cameras as _cam_svc
                payload["config"]["cameras"] = [
                    {"name": c.get("name", ""),
                     # A Reolink camera has no stored snapshot_url; compose the
                     # credentialed one here so the deck (which fetches on the LAN)
                     # can show it. The login stays on the device, never the page.
                     "snapshot_url": (_cam_svc.reolink_snapshot_from_entry(c)
                                      if c.get("source") == "reolink"
                                      else c.get("snapshot_url", "")),
                     "ha_entity": c.get("ha_entity", "")}
                    for c in settings.streamdeck_cameras if isinstance(c, dict)
                ]
                if settings.is_satellite():
                    payload["config"]["weather_location"] = settings.streamdeck_weather_location
                    payload["config"]["weather_units"] = settings.streamdeck_weather_units
            async with bridge_client(timeout=3.0) as c:
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


def _merge_reolink_secrets(incoming: list) -> list:
    """Carry a stored Reolink password onto a re-saved camera list.

    A Reolink entry keeps its login on the server, so the Cameras pane rebuilds
    the list from the visible rows without a password. For each incoming Reolink
    entry that has no password, restore the one stored for the same camera
    (matched by host, channel, and username), so re-saving does not blank the
    credentials. Non-Reolink entries pass through untouched."""
    stored = {}
    for c in settings.streamdeck_cameras or []:
        if isinstance(c, dict) and c.get("source") == "reolink" and c.get("password"):
            key = (str(c.get("host", "")).strip().rstrip("/"),
                   str(c.get("channel", 0)), str(c.get("username", "")).strip())
            stored[key] = c.get("password")
    out = []
    for c in incoming:
        if isinstance(c, dict) and c.get("source") == "reolink" and not c.get("password"):
            key = (str(c.get("host", "")).strip().rstrip("/"),
                   str(c.get("channel", 0)), str(c.get("username", "")).strip())
            if key in stored:
                c = {**c, "password": stored[key]}
        out.append(c)
    return out


class HaCameraDiscoverPayload(BaseModel):
    # Optional overrides so a freshly typed (not yet saved) base/token can be
    # tested before the user commits them. Blank falls back to saved settings.
    base_url: str = ""
    token: str = ""


class CameraScanPayload(BaseModel):
    cidr: str = ""   # blank = this server's own /24


@router.post("/cameras/scan")
async def scan_ip_cameras(payload: CameraScanPayload = CameraScanPayload()):
    """Scan the LAN for IP cameras (FoodAssistant-d9rx).

    Probes each host for common camera ports and, for HTTP hosts, well-known
    snapshot paths, returning candidates the user can preview and add. Runs the
    blocking sweep in a thread so the event loop stays free."""
    import anyio
    from ..services import camera_scan, lan_scan
    # Same resolution as the device scan: explicit, then a remembered range, this
    # host's LAN interface, and a Grocy/Mealie URL host, skipping Docker subnets.
    cidr = lan_scan.resolve_lan_cidr(payload.cidr or "", candidates=[camera_scan.best_lan_cidr()])
    if not cidr:
        return {"ok": False, "error": "Could not determine the local network; enter a CIDR like 192.168.1.0/24."}
    # Remember a good (non-Docker) range so the next blank scan (camera or device)
    # reuses it without the user retyping it.
    if not lan_scan.looks_dockerish(cidr) and cidr != settings.lan_scan_cidr:
        try:
            settings.save({"lan_scan_cidr": cidr})
        except Exception:
            pass
    result = await anyio.to_thread.run_sync(lambda: camera_scan.scan_for_cameras(cidr))
    if result.get("error"):
        return {"ok": False, "error": result["error"]}
    cameras = result.get("cameras", [])
    responded = result.get("responded", 0)
    scanned = result.get("scanned", 0)
    # When we had to guess and the guess is a Docker bridge subnet, tell the user
    # to enter their real LAN (the app runs in a container, FoodAssistant-d9rx).
    hint = ""
    if not (payload.cidr or "").strip() and camera_scan.looks_dockerish(cidr):
        hint = (f"{cidr} looks like a Docker network, not your LAN. Enter your "
                f"home network instead, e.g. 192.168.1.0/24.")
    # Diagnostic note so 'no cameras' is actionable: nothing answering at all on a
    # subnet the user is sure has cameras usually means this container cannot
    # route to the LAN (run with host networking, or add by IP).
    note = ""
    if not cameras:
        if responded == 0:
            note = (f"Scanned {scanned} hosts on {cidr} and nothing answered on camera "
                    "ports. If cameras are definitely on this network, this app is running "
                    "in a Docker container and probably cannot reach your LAN. Run it with "
                    "host networking (network_mode: host), or add the camera by IP above.")
        else:
            note = (f"Found {responded} host(s) with camera-like ports open, but none "
                    "exposed a recognized snapshot path. Add one by IP above using the "
                    "closest brand template.")
    return {"ok": True, "cidr": cidr, "cameras": cameras, "responded": responded,
            "scanned": scanned, "hint": hint, "note": note}


class CameraProbePayload(BaseModel):
    ip: str = ""
    username: str = ""
    password: str = ""


@router.post("/cameras/probe")
async def probe_ip_camera(payload: CameraProbePayload):
    """Re-probe one scanned camera with login credentials (FoodAssistant-ij6w).

    For a password-protected camera the scan can only report that it needs a
    login; with the user's credentials this finds a working snapshot path, reads
    the brand and resolution, and returns a snapshot URL with the credentials
    embedded so it previews and saves without a separate login step."""
    import anyio
    from ..services import camera_scan
    ip = (payload.ip or "").strip()
    if not ip:
        return {"ok": False, "error": "No camera IP given."}
    result = await anyio.to_thread.run_sync(
        lambda: camera_scan.probe_with_auth(ip, payload.username, payload.password))
    return result


@router.get("/cameras/scan-default")
async def scan_default_cidr():
    """The CIDR the camera scan would default to, so the UI can pre-fill and
    show it before scanning (FoodAssistant-d9rx)."""
    from ..services import camera_scan, lan_scan
    # Resolve like the actual scan does (remembered range, LAN interface, then a
    # Grocy/Mealie URL host) so the pre-filled default is the real LAN, not Docker.
    cidr = lan_scan.resolve_lan_cidr("", candidates=[camera_scan.best_lan_cidr()]) or ""
    return {"cidr": cidr, "dockerish": camera_scan.looks_dockerish(cidr) if cidr else False}


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


@router.get("/ha/entities")
async def ha_actionable_entities():
    """Home Assistant entities for the custom-key builder (FoodAssistant-yzjr).

    Fetches HA ``/api/states`` with the stored connection and returns the
    entities a key press can act on (lights, switches, scenes, scripts, ...),
    grouped by domain with each entity's friendly name, current state, and a
    sensible default service. Degrades to an empty list with a hint when the
    connection is not configured, so the builder falls back to free-text
    fields."""
    from ..services import ha_actions
    base = (settings.streamdeck_ha_base_url or "").strip()
    token = (settings.streamdeck_ha_token or "").strip()
    if not base or not token:
        return {"ok": True, "configured": False, "groups": [],
                "hint": "Connect Home Assistant first (Settings, Home Assistant) to pick your devices here."}
    entities = await ha_actions.list_actionable_entities()
    return {"ok": True, "configured": True,
            "groups": ha_actions.group_by_domain(entities)}


class FrigateDiscoverPayload(BaseModel):
    base_url: str = ""


@router.post("/cameras/frigate")
async def frigate_discover_cameras(payload: FrigateDiscoverPayload):
    """Discover cameras from a Frigate NVR (FoodAssistant-7ror).

    Fetches Frigate's ``/api/config`` and returns each camera with a still and a
    stream URL, mirroring the Home Assistant camera discovery. Frigate carries no
    login, so the URLs hold no secret and the browser can preview them; adding one
    stores a plain camera entry the existing resolve/proxy path serves. Fails
    soft with a clear message when Frigate is unreachable or lists no cameras."""
    import anyio
    from ..services import frigate
    result = await anyio.to_thread.run_sync(lambda: frigate.discover(payload.base_url))
    return result


class ReolinkAddPayload(BaseModel):
    name: str = ""
    host: str = ""
    port: str = ""
    channel: int = 0
    username: str = ""
    password: str = ""
    stream_quality: str = "main"   # "main" (full) or "sub" (lighter)
    test: bool = True              # verify the snapshot before saving


def _reolink_entry_from_payload(payload: ReolinkAddPayload) -> dict:
    """Compose a Reolink camera entry from the add/preview form fields.

    The login rides the entry only server-side; callers strip the password
    before handing anything back to the page."""
    host = (payload.host or "").strip().rstrip("/")
    return {
        "name": (payload.name or host).strip(),
        "source": "reolink",
        "host": host,
        "port": (payload.port or "").strip(),
        "channel": payload.channel,
        "username": (payload.username or "").strip(),
        "password": payload.password or "",
        "stream_quality": "sub" if str(payload.stream_quality).lower() == "sub" else "main",
    }


async def _reolink_snapshot_or_error(entry: dict):
    """Fetch a Reolink still with the real token sign-in the camera proxy uses.

    Returns ``(content, content_type, None)`` on a good image, or
    ``(None, None, message)`` with a friendly rejected/unreachable/no-snapshot
    message on failure. The token and login stay on the server. Shared by the
    preview and add flows so both behave identically."""
    from ..services import cameras as cam_svc
    try:
        status, content, ctype = await cam_svc.fetch_reolink_snapshot(entry)
    except cam_svc.ReolinkAuthError:
        return None, None, "The camera rejected that username or password."
    except Exception as e:
        return None, None, f"Could not reach the camera: {e}"
    if not cam_svc._looks_like_jpeg(status, content, ctype):
        return None, None, f"The camera did not return a snapshot (HTTP {status})."
    return content, ctype, None


@router.post("/cameras/reolink/preview")
async def preview_reolink_camera(payload: ReolinkAddPayload):
    """Preview a Reolink camera before adding it (FoodAssistant-26mf).

    Signs in with the entered host, channel, and login and streams back the
    still image bytes, so the setup page can confirm the feed and the login are
    right before saving, the same way the Frigate and IP-scan flows offer a
    Preview. Nothing is saved. The login is posted in the request body and the
    token stays on the server, so no credential ever appears in a browser URL or
    the page source. Failures come back as the same rejected, unreachable, or
    no-snapshot messages the Add verify returns."""
    from fastapi.responses import Response
    entry = _reolink_entry_from_payload(payload)
    if not entry["host"]:
        return JSONResponse({"ok": False, "error": "Enter the camera's address."},
                            status_code=400)
    content, ctype, error = await _reolink_snapshot_or_error(entry)
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=502)
    return Response(content=content, media_type=ctype or "image/jpeg")


@router.post("/cameras/reolink")
async def add_reolink_camera(payload: ReolinkAddPayload):
    """Add a Reolink camera by host, channel, and login (FoodAssistant-qft4).

    The credentials are kept on the server: the snapshot and RTSP URLs are
    composed here (never sent to the browser) and the saved entry is fetched
    server-side by the camera proxy, so no login ever reaches the page.
    Best-effort probes the camera's device info to recognise a doorbell model
    and flag two-way-talk capability (``device_type``/``two_way_talk`` on the
    stored entry); a failed probe just leaves those unset, it never blocks
    adding the camera. Two-way audio itself is not implemented (it needs a
    WebRTC session); this only surfaces the capability. Optionally verifies
    the still image before saving so a wrong login is caught up front. Returns
    the stored entry without the password."""
    entry = _reolink_entry_from_payload(payload)
    if not entry["host"]:
        return {"ok": False, "error": "Enter the camera's address."}
    if payload.test:
        # Verify with the real token sign-in the camera proxy uses, so a wrong
        # login is caught up front. The credentials never leave the server.
        _content, _ctype, error = await _reolink_snapshot_or_error(entry)
        if error:
            return {"ok": False, "error": error}
    try:
        from ..services import cameras as cam_svc, camera_detect
        dev_info = await cam_svc.fetch_reolink_dev_info(entry)
        caps = camera_detect.reolink_capabilities(dev_info)
        entry["device_type"] = "doorbell" if caps["is_doorbell"] else "camera"
        entry["two_way_talk"] = caps["two_way_talk"]
    except Exception:
        pass  # capability detection is a nicety, never blocks adding the camera
    cams = [c for c in (settings.streamdeck_cameras or []) if isinstance(c, dict)]
    cams.append(entry)
    settings.save({"streamdeck_cameras": cams})
    # Hand back the entry without the secret so the page can show a row.
    safe = {k: v for k, v in entry.items() if k != "password"}
    return {"ok": True, "camera": safe}


@router.get("/streamdeck/actions")
async def streamdeck_actions():
    """List assignable Stream Deck actions for the grid editors.

    On a Pi the live catalog comes from the host bridge. Everywhere else (and
    when the bridge is unreachable) the bundled generated catalog is served, so
    the Start Page editor on a plain server shows the same full palette a Pi
    gets."""
    if is_raspberry_pi():
        try:
            async with bridge_client(timeout=12.0) as c:
                r = (await c.get(f"{_HOST_BRIDGE}/streamdeck/actions")).json()
            if r.get("ok") and isinstance(r.get("actions"), list):
                return r
        except Exception:
            pass
    from ..services.start_page import bundled_catalog
    actions = bundled_catalog()
    if actions:
        return {"ok": True, "actions": actions, "source": "bundled"}
    return {"ok": False, "error": "Action catalog unavailable."}


@router.post("/streamdeck/install")
async def streamdeck_install():
    """Provision the Stream Deck service for a deck attached after first install."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    try:
        async with bridge_client(timeout=15.0) as c:
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
        async with bridge_client(timeout=4.0) as c:
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
        async with bridge_client(timeout=15.0) as c:
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


def _abs_axis(fd: int, axis: int, default_max: int = 4095) -> tuple[int | None, int, int]:
    """Return (value, min, max) for an absolute axis via EVIOCGABS.

    value is the axis position the device last reported (None when the ioctl
    fails); min/max fall back to a sane default range.
    """
    try:
        buf = bytearray(_ABSINFO_SIZE)
        fcntl.ioctl(fd, _eviocgabs(axis), buf, True)
        value, minimum, maximum, _fuzz, _flat, _res = struct.unpack(_ABSINFO_FORMAT, bytes(buf))
        if maximum > minimum:
            return value, minimum, maximum
    except OSError:
        pass
    return None, 0, default_max


def _fold_touch_events(data: bytes, x: int | None, y: int | None):
    """Fold a raw evdev read buffer into completed taps.

    Returns (taps, x, y): one (x, y) tuple per BTN_TOUCH release seen in the
    buffer, plus the updated last-known axis values to carry into the next
    read. The position is deliberately carried across taps rather than reset:
    the kernel input core suppresses ABS events whose value did not change, so
    a tap in line with the previous contact (same raw X or Y) arrives with no
    fresh coordinate for that axis. Resetting to None after each tap dropped
    exactly those releases (FoodAssistant-9ext: tap on a crosshair silently
    not registered).
    """
    taps: list[tuple[int, int]] = []
    for off in range(0, len(data) - _INPUT_EVENT_SIZE + 1, _INPUT_EVENT_SIZE):
        _s, _u, etype, code, value = struct.unpack(
            _INPUT_EVENT_FORMAT, data[off:off + _INPUT_EVENT_SIZE])
        if etype == _EV_ABS and code == _ABS_X:
            x = value
        elif etype == _EV_ABS and code == _ABS_Y:
            y = value
        elif etype == _EV_KEY and code == _BTN_TOUCH and value == 0 \
                and x is not None and y is not None:
            taps.append((x, y))
    return taps, x, y


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

    x0, x_min, x_max = _abs_axis(fd, _ABS_X)
    y0, y_min, y_max = _abs_axis(fd, _ABS_Y)
    yield "data: " + json.dumps({
        "type": "ranges", "x_min": x_min, "x_max": x_max,
        "y_min": y_min, "y_max": y_max,
    }) + "\n\n"

    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()

    def _read_loop():
        # Seed the position from the axis state at open (EVIOCGABS reports the
        # last value the device sent). The kernel suppresses ABS events whose
        # value did not change, so the first tap can arrive as a bare
        # BTN_TOUCH press/release with no fresh coordinates; without the seed
        # that first tap was silently dropped (FoodAssistant-9ext).
        x, y = x0, y0
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
                taps, x, y = _fold_touch_events(data, x, y)
                for tap in taps:
                    loop.call_soon_threadsafe(queue.put_nowait, tap)
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
        async with bridge_client(timeout=40.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/touch/calibrate/reset")
        # An old host bridge predates this route and answers 404
        # {"error": "not found"}; say something actionable instead.
        if r.status_code == 404:
            return JSONResponse({"ok": False, "error":
                "The device helper software is out of date. Press Update "
                "under Backup & Updates, then try again."})
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
        async with bridge_client(timeout=4.0) as c:
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
        async with bridge_client(timeout=20.0) as c:
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
        # A stale bridge (updated app, old bridge) delegated to a separate
        # calibrate helper and answered "calibrate helper not installed"
        # (FoodAssistant-jppi). The current bridge writes the rule itself and
        # the updater refreshes the bridge, so the fix is one Update away.
        err = data.get("error", f"HTTP {r.status_code}")
        if "not installed" in err.lower():
            err = ("The device helper software is out of date. Press Update "
                   "under Backup & Updates, then calibrate again.")
        return JSONResponse({"ok": False, "error": err})
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
