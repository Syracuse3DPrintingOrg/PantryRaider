"""The /setup page itself and the context helpers that render it."""

import asyncio
import os
from pathlib import Path
import httpx
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from ...config import (
    settings, APP_VERSION, GITHUB_REPO, THEMES, UI_SCALES, DISPLAY_ROTATIONS,
    DISPLAY_TYPES, HARDWARE_PRESETS, COMMON_TIMEZONES, format_local, DEPLOYMENT_MODES,
    _DEFAULT_DEPLOYMENT_MODE, AI_MODELS, SATELLITE_READONLY_FIELDS, KITCHEN_APPLIANCES,
    browser_host, device_hostname, resolve_custom_colors, active_custom_name,
)
from ...hardware import is_raspberry_pi, board_model, supports_local_stack
from ...navigation import all_tabs, default_tabs, split_pane_pages
from ...services import tunnel as tunnel_service
from ...services.host_panel import mealie_action_state
from ...storage_categories import custom_categories
from ...templating import templates
from ._common import router, _SECRET_FIELDS, _ha_slot_review


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
    """Return the first candidate Grocy URL that responds with a 200/401, or ''.

    All three candidates are probed at once with a short per-probe timeout
    (FoodAssistant-0hwez): probing them one after another put up to 4.5s of
    connect timeouts inside the settings render on a box where none answer.
    The candidate list's order still decides which hit wins."""
    async def _probe(client: httpx.AsyncClient, url: str) -> bool:
        try:
            r = await client.get(f"{url}/api/system/info")
            return r.status_code in (200, 401)
        except Exception:
            return False

    async with httpx.AsyncClient(timeout=0.75) as client:
        hits = await asyncio.gather(
            *(_probe(client, url) for url in _LOCAL_GROCY_CANDIDATES))
    for url, hit in zip(_LOCAL_GROCY_CANDIDATES, hits):
        if hit:
            return url
    return ""


def _printing_available() -> bool:
    """True when the device's print stack (CUPS / lp tools) is installed.

    Wrapped so the setup page never hard-fails if the printing service cannot
    import for any reason: a missing print stack just reads as unavailable."""
    try:
        from ...services.printing import printing_available
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
        from ...config import _lan_ip
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
    # Mealie card affordance (FoodAssistant-mvke): whether Mealie is already
    # up on this device. The live answer comes from the host bridge running a
    # docker compose ps, which can take seconds on a Pi's SD card, so it moved
    # OUT of this render (FoodAssistant-0hwez): the page paints with the
    # running affordances hidden, then panes.js asks GET /setup/mealie/status
    # and reveals them when Mealie really is up. A satellite points at a
    # remote stack and never starts Mealie here.
    mealie_available = is_raspberry_pi() and not settings.is_satellite()
    mealie_state = ""
    mealie_action = mealie_action_state(
        installed=False, running=False, available=mealie_available)
    # LAN link to the barcode-scanner setup wizard, encoded as a QR on the
    # Scanning pane so a user can open the wizard on a phone by the reader
    # instead of at the kiosk (FoodAssistant-udpk).
    from ..qr import lan_url_for
    from ...services import recipe_source
    scanner_wizard_url = lan_url_for(request, "/ui/scanner-setup")
    upd = await _update_path_context()
    return templates.TemplateResponse(request, "setup.html", {
        "request": request,
        "s": settings,
        "configured": settings.is_configured(),
        "scanner_wizard_url": scanner_wizard_url,
        # Whether this install can bring the Cloudflare helper up on its own. A
        # Pi appliance can (the host bridge does it, as root, outside the
        # container); a Compose install cannot, because the app image has no
        # Docker CLI and no compose file mounts the socket. The pane offers the
        # manual route instead of a button that would only ever fail.
        "cloudflare_self_hosted": (settings.is_pi_appliance()
                                   or tunnel_service.cloudflare_available()),
        # booleans only: never the stored secrets themselves
        "has": {f: bool(getattr(settings, f, "")) for f in _SECRET_FIELDS},
        # count of stored extra keys per provider (values never sent to the page)
        "extra_key_counts": {
            p: len([k for k in (settings.ai_extra_keys.get(p, []) if isinstance(settings.ai_extra_keys, dict) else []) if k])
            for p in ("gemini", "openai", "anthropic")
        },
        "ai_models": AI_MODELS,
        "tabs": all_tabs(),
        "tabs_default": default_tabs(),
        # On-screen Start Page editor (Pantry Raider): the shared custom buttons
        # (same store as the Stream Deck). The built-in key catalog is the deck's
        # own (_sdCatalog, loaded client-side), so the two editors are identical.
        "start_customs": _start_customs(),
        # The older HA 1 to HA 5 keys the 0.21 key rules flagged, which the
        # Start Page pane lists with Allow, Stop and Remove.
        "ha_slot_review": _ha_slot_review(),
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
        # Pages a split-view pane can start on (FoodAssistant-dqpq), for the
        # Split screen dropdowns on the Screen pane.
        "split_pages": split_pane_pages(),
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
        # Live Mealie affordance for the Pi host card: "running" | "none"
        # (FoodAssistant-mvke). Always "none" at render time now; panes.js
        # fetches /setup/mealie/status after paint and reveals the running
        # affordances (marked data-mealie-running) when Mealie is up.
        "mealie_action": mealie_action,
        "mealie_state": mealie_state,
        # Whether the page should ask for the live Mealie state at all (a Pi
        # appliance that is not a satellite).
        "mealie_probe": mealie_available,
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
        # A server with the Watchtower token set has an updater Update now can
        # trigger; without it (Unraid, a hand-rolled compose) the container's
        # own tool updates it, so the pane hides the button.
        "has_updater": bool(os.environ.get("WATCHTOWER_HTTP_API_TOKEN")),
        # How this install updates (services/update_path.py): update_kind is
        # pi, watchtower, server_bridge or container; bridge_updates is True
        # where the host bridge installs updates (a Pi, or an install.sh
        # server whose bridge proved itself).
        **upd,
        # False until a channel is saved: installs that never chose one now
        # follow tested releases. An updated install is told so until the
        # owner dismisses the note or saves a channel.
        "update_channel_explicit": _channel_explicit(),
        "update_channel_note": _channel_note(),
        # The Home Assistant account line and the screens that still keep a
        # copy of the token (names only).
        "ha_status": _ha_status_context(),
        "ha_token_holders": _ha_token_holders(),
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
        "satellite_managed": SATELLITE_READONLY_FIELDS,
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
        # this is False (FoodAssistant-fb8x). In a worker thread because the
        # probe shells out to lpstat, which must not sit on the event loop
        # (FoodAssistant-0hwez).
        "printing_available": await run_in_threadpool(_printing_available),
        # The print service's state and which fix the Printing pane offers
        # for an older one (cached, no I/O).
        "print_service": _print_service_value("print_service_state", "unknown"),
        "print_service_fix": _print_service_value("print_service_fix_kind", "manual"),
    })


async def _update_path_context() -> dict:
    from ...services import update_path
    try:
        r = await update_path.resolve()
    except Exception:  # noqa: BLE001 - the pane still renders
        r = {"kind": update_path.KIND_CONTAINER, "bridge": update_path.BRIDGE_ABSENT,
             "bridge_hint": False}
    return {
        "update_kind": r.get("kind"),
        "bridge_status": r.get("bridge"),
        "bridge_updates": bool(settings.is_pi_appliance()
                               or r.get("kind") == update_path.KIND_SERVER_BRIDGE),
        "bridge_hint": bool(r.get("bridge_hint")),
    }


def _channel_explicit() -> bool:
    try:
        return bool(settings.update_channel_is_explicit())
    except Exception:  # noqa: BLE001
        return True


def _channel_note() -> bool:
    """Whether Backups & Updates says that this kitchen now follows tested
    releases: only on an install that was updated to 0.21 without ever
    choosing a channel (a new install never followed anything else)."""
    if _channel_explicit():
        return False
    try:
        from ...services import auth_state
        return auth_state.origin() == "upgraded"
    except Exception:  # noqa: BLE001
        return False


def _ha_status_context() -> dict:
    try:
        from ...services import ha_client
        return ha_client.status_snapshot()
    except Exception:  # noqa: BLE001
        return {}


def _ha_token_holders() -> list:
    """Names of satellites that still receive the Home Assistant token.

    Empty when this install holds no token of its own, so there is nothing a
    satellite could be receiving."""
    try:
        if settings.is_satellite() or not str(settings.streamdeck_ha_token or "").strip():
            return []
        from ...services import devices
        out = []
        for row in devices.list_devices():
            if not isinstance(row, dict):
                continue
            if row.get("deployment_mode") != "pi_remote" or row.get("source") != "heartbeat":
                continue
            if row.get("relay") and row.get("deck_proxy"):
                continue
            name = str(row.get("label") or row.get("hostname") or row.get("device_id") or "")
            if name:
                out.append(name)
        return out
    except Exception:  # noqa: BLE001
        return []


def _print_service_value(fn_name: str, default: str) -> str:
    try:
        from ...services import printing
        return str(getattr(printing, fn_name)())
    except Exception:  # noqa: BLE001
        return default


def _start_customs() -> list[dict]:
    from ...services import start_page
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
