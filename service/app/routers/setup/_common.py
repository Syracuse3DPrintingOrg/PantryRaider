"""Shared pieces of the /setup routes: the router, the SetupPayload model,
secret and key-merging helpers, and the host-bridge helpers more than one
route module calls."""

from pathlib import Path
from fastapi import APIRouter
from pydantic import BaseModel, create_model

from ...config import settings, Settings, setting_meta
from ...hardware import is_raspberry_pi
from ...navigation import NAV_TABS, CUSTOM_PREFIX
from ...services.bridge import bridge_client

router = APIRouter(prefix="/setup", tags=["setup"])


# Saved values for these are never rendered back into the page. The form
# sends "" to keep the stored value and "__CLEAR__" to erase it.
_SECRET_FIELDS = [name for name, info in Settings.model_fields.items()
                  if info.annotation is str and {"secret", "form"} <= setting_meta(info).keys()]
_CLEAR = "__CLEAR__"
_DOCUMENT_PRINT_CHOICES = {
    "document_page_size": ("DOCUMENT_PAGE_SIZES", "auto"),
    "document_color_mode": ("DOCUMENT_COLOR_MODES", "color"),
    "document_duplex": ("DOCUMENT_DUPLEX_MODES", "one-sided"),
}


def _clean_custom_nav_tabs(submitted) -> tuple[list[dict], list[dict]]:
    """Validate posted custom nav tabs into clean stored dicts.

    Delegates to url_safety.clean_custom_tabs, the one URL policy: returns
    (clean, rejected), where clean is the stored shape {id,label,icon,url,
    parent,heading} with stable, de-duplicated ids (rows with no label are
    dropped as before) and rejected lists each tab whose address is not
    allowed as {id, label, message}.
    """
    from ...services.url_safety import clean_custom_tabs
    return clean_custom_tabs(submitted if isinstance(submitted, list) else [])


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


def _refuse_probe_target(url: str) -> str:
    """Why a connection test must not fetch this URL, or "" when it is fine.

    The connection tests are reachable WITHOUT authentication during the
    unconfigured window (they sit in the setup bypass so a fresh device can be
    set up at all) and they fetch an address the caller supplies. That
    combination is a server-side request forgery primitive: without a guard,
    anyone who can reach a not-yet-configured device can make it probe
    addresses they cannot reach themselves (FoodAssistant-3okp).

    Reuse the camera egress guard, which resolves every address the name maps
    to (so a name pointing partly at loopback is caught) and refuses loopback,
    link-local (which covers the cloud metadata address), multicast, reserved,
    and unspecified, while leaving ordinary LAN and public hosts alone. A real
    backend is a sibling container or a host on the network, never this app's
    own loopback: the shipped defaults are http://grocy:80 and
    http://ollama:11434. Blocking loopback also closes the worst case, because
    the app trusts a loopback client as an administrator, so a fetch must never
    be aimable at the app itself.

    Unresolvable names are allowed through (fail-open): they cannot be fetched
    anyway, and refusing them would turn a momentary DNS hiccup into a
    confusing "not allowed" during setup.
    """
    if not url:
        return ""
    from ...services.cameras import is_blocked_fetch_host
    if is_blocked_fetch_host(url, fail_closed=False):
        return ("That address is not allowed. Point this at the backend's own "
                "address on your network, not at this device itself.")
    return ""


# The /setup/save request is built from the Settings fields declared with
# setting(form=...) in config.py, so a new setting reaches this endpoint by
# being declared there. The save handler reads payload.model_dump(
# exclude_unset=True), so only the fields a page actually posts are applied.
#
# The fields below accept a different shape than the stored setting. Each is
# (type, default), used in place of the one derived from Settings. A default
# here only shows on the model itself: exclude_unset means a field the page
# did not post never reaches the save.
_SETUP_PAYLOAD_OVERRIDES: dict[str, tuple] = {
    "ollama_base_url": (str, ""),
    "grocy_base_url": (str, ""),
    "themealdb_api_key": (str, ""),
    # Extra API keys per provider, e.g. {"gemini": ["key2", "key3"]}. When
    # absent the stored extras are left untouched (see save handler).
    "ai_extra_keys": (dict[str, list[str]] | None, None),
    # Each row is {"key": <secret or __KEEP__:i>, "name": <label>}; a bare
    # string is still accepted (see _merge_satellite_keys).
    "extra_api_keys": (list[dict | str] | None, None),
    # Lists the handler cleans and validates. None = the field was not
    # submitted, so the stored value is kept; [] = explicitly none.
    "cub_rotation": (list[str] | None, None),
    "custom_nav_tabs": (list[dict] | None, None),
    "kitchen_appliances": (list[str] | None, None),
    "weather_api_base": (str, ""),
    "streamdeck_key_style": (str, ""),
    "streamdeck_icon_color": (str, ""),
    "floating_nav_position": (str, ""),
    "floating_nav_orientation": (str, ""),
    "nav_visibility": (str, ""),
    "timer_chips": (str, ""),
    "clock_format": (str, ""),
    "barcode_autocheck_shopping": (bool, False),
    "start_page_enabled": (bool, False),
    "floating_nav_autohide_streamdeck": (bool, False),
    "stemma_enabled": (bool, False),
}

# Request-only fields with no stored setting of their own.
_SETUP_PAYLOAD_EXTRAS: dict[str, tuple] = {
    # Custom-key definitions built in the Start Page editor; merged into the
    # shared streamdeck_key_overrides store (deck slots preserved) by the handler.
    "start_custom_defs": (list | None, None),
    "start_loaded_ids": (list | None, None),
    # Required to change or remove an existing auth_password.
    "current_password": (str, ""),
    # The wizard's Remote Access choice: turn Forager remote access on as part
    # of the save that completes setup (see save_setup).
    "forager_remote_access": (bool | None, None),
}


def _setup_payload_fields() -> dict[str, tuple]:
    fields: dict[str, tuple] = {}
    for name, info in Settings.model_fields.items():
        form = setting_meta(info).get("form")
        if not form:
            continue
        if name in _SETUP_PAYLOAD_OVERRIDES:
            fields[name] = _SETUP_PAYLOAD_OVERRIDES[name]
        elif form == "optional":
            fields[name] = (info.annotation | None, None)
        else:
            fields[name] = (info.annotation, info.get_default(call_default_factory=True))
    fields.update(_SETUP_PAYLOAD_EXTRAS)
    return fields


SetupPayload = create_model("SetupPayload", **_setup_payload_fields())


class TestGrocyPayload(BaseModel):
    grocy_base_url: str = ""
    grocy_api_key: str = ""


class TestMealiePayload(BaseModel):
    mealie_base_url: str = ""
    mealie_api_key: str = ""


# The Pi host bridge. Many setup routes call a small helper service running on 127.0.0.1:9299 on the Pi host.
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


def _ha_slot_review() -> list:
    """The older HA 1 to HA 5 keys that need the owner's decision (the Start
    Page pane lists them). [] on a satellite, whose keys are its server's."""
    if settings.is_satellite():
        return []
    try:
        from ...services import ha_allowlist
        return ha_allowlist.slot_review(settings.streamdeck_ha_slots or [])
    except Exception:  # noqa: BLE001 - the pane still loads without the list
        return []


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


def _normalized_calls(entry, slots) -> list:
    from ...services import ha_allowlist, ha_policy
    return sorted(ha_policy.normalize(e, s) for e, s in ha_allowlist.entry_calls(entry, slots))


def _keep_unchanged_ha_flags(incoming, stored, slots_in, slots_stored, *, match: str):
    """(entries, unchanged) for a posted list of overrides or slots.

    unchanged holds the (id, slot) reference of every posted entry that
    repeats a stored entry's Home Assistant calls exactly (matched by override
    id, or by position for slots). Such an entry keeps the stored
    disabled_reason when the editor that posted it does not know the field.
    """
    from ...services import ha_allowlist
    if not isinstance(incoming, list):
        return incoming, set()
    stored = list(stored) if isinstance(stored, list) else []

    def calls(entry, slots):
        try:
            return sorted(repr(c) for c in ha_allowlist.entry_calls(entry, slots))
        except Exception:  # noqa: BLE001 - when unsure, treat it as changed
            return None

    by_id = {}
    if match == "id":
        for e in stored:
            if isinstance(e, dict) and str(e.get("id") or ""):
                by_id.setdefault(str(e.get("id")), e)
    out, unchanged = [], set()
    for i, entry in enumerate(incoming):
        if not isinstance(entry, dict):
            out.append(entry)
            continue
        if match == "id":
            old = by_id.get(str(entry.get("id") or "")) if entry.get("id") else None
        else:
            old = stored[i] if i < len(stored) else None
        if isinstance(old, dict):
            mine = calls(entry, slots_in)
            if mine and mine == calls(old, slots_stored):
                unchanged.add((str(entry.get("id") or ""), str(entry.get("slot", i))))
                if "disabled_reason" in old and "disabled_reason" not in entry:
                    entry = dict(entry, disabled_reason=old["disabled_reason"])
        out.append(entry)
    return out, unchanged


def _carry_elevated_optin(incoming, stored, slots_in, slots_stored, *, match: str) -> list:
    """Keep the owner's allow_elevated choice through a save from an editor
    that does not know it.

    An editor opened before the key policy existed posts its keys without
    allow_elevated. For each incoming entry that matches a stored one (by
    override id, or by position for slots) and makes exactly the same Home
    Assistant calls, allow_elevated and elevated_grandfathered are copied
    from the stored entry. A changed entity or service never inherits it.
    """
    if not isinstance(incoming, list):
        return incoming
    stored = [e for e in (stored or [])] if isinstance(stored, list) else []
    by_id = {}
    if match == "id":
        for e in stored:
            if isinstance(e, dict) and str(e.get("id") or ""):
                by_id.setdefault(str(e.get("id")), e)
    out = []
    for i, entry in enumerate(incoming):
        if not isinstance(entry, dict) or "allow_elevated" in entry:
            out.append(entry)
            continue
        if match == "id":
            old = by_id.get(str(entry.get("id") or "")) if entry.get("id") else None
        else:
            old = stored[i] if i < len(stored) else None
        if not isinstance(old, dict) or "allow_elevated" not in old:
            out.append(entry)
            continue
        try:
            same = _normalized_calls(entry, slots_in) == _normalized_calls(old, slots_stored)
        except Exception:  # noqa: BLE001 - when unsure, do not carry it
            same = False
        if same:
            entry = dict(entry)
            for k in ("allow_elevated", "elevated_grandfathered"):
                if k in old:
                    entry[k] = old[k]
        out.append(entry)
    return out


# Kiosk navigate-to-dashboard. When the setup wizard is finished from a remote browser, the Pi's attached
# kiosk display is still sitting on the wizard page. It cannot navigate itself,
# so save_setup raises this flag when a save completes setup, and the kiosk
# pollers (base.html on the app pages, setup.html on the wizard splash and the
# settings page) pick it up and drive their own display to the dashboard
# (FoodAssistant-6v9q). Same one-shot flag pattern as the touch-calibration
# flow in calibrate.py.
_KIOSK_NAV_FLAG = Path(settings.data_dir) / "kiosk_navigate.flag"


def _write_kiosk_nav_flag() -> str | None:
    """Raise the one-shot kiosk hand-off flag. Returns an error string when the
    flag cannot be written (best effort: no caller blocks on it)."""
    try:
        _KIOSK_NAV_FLAG.parent.mkdir(parents=True, exist_ok=True)
        _KIOSK_NAV_FLAG.write_text("1")
    except OSError as e:
        return str(e)
    return None
