"""Home Assistant integration API (FoodAssistant-ju93).

The endpoints that let the Home Assistant integration mirror this install's
state, push a few kiosk display settings back, and hand over (or take back)
the Home Assistant connection, without HA having to know about Grocy, the
host bridge, or the fleet's satellite/server split directly. Who may call
them is decided in one place, the access rules behind the require_auth
middleware (these routes are the 'ha' family: Home Assistant and legacy keys,
and keyless home callers while an open install still allows them), so there
is no gating here.

Every value in GET /ha/state degrades to a calm empty/zero on a backend
failure rather than a 500: a Home Assistant sensor reading this endpoint every
few seconds should never see the integration itself go down because Grocy, the
host bridge, or a thermometer reader happened to be unreachable this poll.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel

from ..config import (
    APP_VERSION,
    SCREENSAVER_MODES,
    device_hostname,
    settings,
)
from ..hardware import is_raspberry_pi
from ..services.bridge import bridge_client

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ha", tags=["home-assistant"])

_HOST_BRIDGE = "http://127.0.0.1:9299"
_MAX_ACTIVE_TIMERS = 20


# -- GET /ha/state ------------------------------------------------------------


def _display_block() -> dict:
    return {
        "idle_timeout": settings.display_idle_timeout,
        "screensaver_minutes": settings.screensaver_minutes,
        "screensaver_mode": settings.screensaver_mode,
        # Not a Settings field yet (lands separately); getattr keeps this
        # endpoint working before and after that field exists.
        "wake_on_presence": getattr(settings, "wake_on_presence", "auto"),
    }


async def _presence_block() -> dict:
    """Best-effort presence from the bridge's /hardware/status (the endpoint
    that carries the LD2410C state, FoodAssistant-6z8c). Off a Pi, or on any
    failure/absence, this degrades to "nothing detected" rather than erroring,
    since presence is an optional enrichment, not core state."""
    if not is_raspberry_pi():
        return {"available": False, "detected": False}
    try:
        from ..services.bridge import bridge_client
        async with bridge_client(timeout=1.5) as c:
            r = await c.get(f"{_HOST_BRIDGE}/hardware/status")
        if r.status_code == 200:
            data = r.json()
            presence = data.get("presence") if isinstance(data, dict) else None
            if isinstance(presence, dict):
                return {
                    "available": bool(presence.get("available")),
                    "detected": bool(presence.get("detected")),
                }
    except Exception:
        pass
    return {"available": False, "detected": False}


def _printers_block() -> dict:
    """Effective print queues, mirroring routers/printing.py's
    _effective_label_queue / _effective_document_queue resolution. Any
    failure degrades to empty lists/strings, never a 500."""
    from ..services import printing as printing_svc

    try:
        queues = printing_svc.list_queues()
    except Exception:
        queues = []
    try:
        local_label = printing_svc.local_label_queue(settings.label_printer_queue)
        label_queue = local_label or printing_svc.resolve_effective_queue(
            settings.label_printer_queue, settings.fleet_label_printer_queue)
    except Exception:
        label_queue = ""
    try:
        document_queue = printing_svc.resolve_effective_queue(
            settings.document_printer_queue, settings.fleet_document_printer_queue)
    except Exception:
        document_queue = ""
    return {
        "label_queue": label_queue,
        "document_queue": document_queue,
        "queues": [
            {"name": q.get("name", ""), "state": q.get("state", "")}
            for q in (queues or []) if isinstance(q, dict)
        ],
    }


async def _expiring_block() -> dict:
    """Expiry bucket counts, sharing the /expiring/count cache so an HA poll
    never adds its own extra Grocy pull. A Grocy failure returns all zeros
    plus expiring_ok: false so the HA sensor can show a clear "unavailable"
    state instead of a silently stale zero."""
    from . import expiring as expiring_router
    from ..services.grocy import GrocyClient

    try:
        all_items = expiring_router._count_items_cache.get()
        if all_items is None:
            grocy = GrocyClient()
            all_items = await grocy.get_expiring(days=30)
            expiring_router._count_items_cache.set(all_items)
        return {
            "expired": sum(1 for i in all_items if i["days_remaining"] < 0),
            "today": sum(1 for i in all_items if i["days_remaining"] == 0),
            "within_3_days": sum(1 for i in all_items if 0 < i["days_remaining"] <= 3),
            "within_7_days": sum(1 for i in all_items if 3 < i["days_remaining"] <= 7),
            "expiring_ok": True,
        }
    except Exception:
        return {"expired": 0, "today": 0, "within_3_days": 0, "within_7_days": 0,
                "expiring_ok": False}


def _counts_block() -> dict:
    from ..database import SessionLocal
    from ..models.db_models import PendingItem
    from ..services import action_items

    db = SessionLocal()
    try:
        pending = db.query(PendingItem).count()
        active = action_items.count_active(db)
    except Exception:
        pending, active = 0, 0
    finally:
        db.close()
    return {"pending": pending, "action_items": active}


def _timers_block() -> dict:
    from ..services import timers as timers_svc

    try:
        all_timers = timers_svc.list_timers()
    except Exception:
        return {"running": 0, "next": None}
    running = [t for t in all_timers if not t.get("expired")]
    running.sort(key=lambda t: t.get("remaining_seconds", 0))
    nxt = None
    if running:
        nxt = {"label": running[0].get("label", ""),
               "remaining_seconds": running[0].get("remaining_seconds", 0)}
    # The full list (running and just-expired), exactly as list_timers
    # returns it, capped so a runaway timer count never bloats a poll an HA
    # sensor hits every few seconds.
    active = [
        {
            "label": t.get("label", ""),
            "remaining_seconds": t.get("remaining_seconds", 0),
            "expired": bool(t.get("expired", False)),
        }
        for t in all_timers[:_MAX_ACTIVE_TIMERS]
    ]
    return {"running": len(running), "next": nxt, "active": active}


def _thermometers_block() -> list[dict]:
    from ..services import gadgets

    try:
        state = gadgets.get_state()
        devices = state.get("devices", []) if isinstance(state, dict) else []
    except Exception:
        return []
    out = []
    for d in devices:
        if not isinstance(d, dict):
            continue
        out.append({
            "id": d.get("id", ""),
            "name": d.get("name", ""),
            "battery": d.get("battery"),
            "stale": bool(d.get("stale", True)),
            "probes": [
                {
                    "index": p.get("index"),
                    "role": p.get("role", ""),
                    "role_label": p.get("role_label", ""),
                    "temp_c": p.get("temp_c"),
                    "target_c": p.get("target_c"),
                }
                for p in d.get("probes", []) if isinstance(p, dict)
            ],
        })
    return out


def _satellites_block() -> list[dict]:
    from ..services import devices as devices_svc

    try:
        rows = devices_svc.list_devices()
    except Exception:
        return []
    return [
        {
            "device_id": r.get("device_id", ""),
            "hostname": r.get("hostname", ""),
            "ip": r.get("ip", ""),
            "version": r.get("version", ""),
            "last_seen": r.get("last_seen", ""),
        }
        for r in rows
    ]


# What this app can do for the Home Assistant integration. The integration
# reads these instead of guessing from the version: ha_token_dedicated means
# /ha/connect stores the token kind and POST /ha/disconnect exists, and
# probe_alert_service means probe alerts go to the integration's service.
HA_FEATURES = ["ha_token_dedicated", "probe_alert_service", "ha_proxy",
               "pairing_kinds", "credential_exchange"]


def _ha_access_block() -> dict:
    """Which rights the integration's Home Assistant account needs, and what
    kind of token this install holds. An error working out the needs answers
    read and control: an account's rights are never shrunk by a failure."""
    needs = ["read", "control"]
    try:
        from ..database import SessionLocal
        from ..services import ha_allowlist

        db = SessionLocal()
        try:
            profiles = ha_allowlist.load_profiles(db)
        finally:
            db.close()
        if not ha_allowlist.needs_control(settings, profiles):
            needs = ["read"]
    except Exception:
        log.debug("could not work out the Home Assistant account's needs", exc_info=True)
        needs = ["read", "control"]
    block = {"needs": needs, "token_kind": "", "token_admin": None, "token_expires": "",
             "token_ref": ""}
    try:
        from ..services import ha_client

        block["token_kind"] = str(settings.ha_token_kind or "")
        block["token_admin"] = settings.ha_token_admin
        block["token_expires"] = str(settings.ha_token_expires or "")
        # Which Home Assistant token this install holds, as a short digest the
        # integration can match against the tokens it made (never the token).
        block["token_ref"] = ha_client.token_ref()
    except Exception:
        pass
    return block


def _auth_block(request: Request) -> dict:
    """How the caller signed in and whether this install still has no
    password (with the end of its grace period, and what the grace is about,
    so the integration can show the right repair in Home Assistant)."""
    block = {"credential": "none", "open": False, "notice": None,
             "grace_until": None, "grace_kind": None}
    try:
        from ..services import access

        block["open"] = not settings.auth_password
        block["notice"] = access.OPEN_NOTICE if block["open"] else None
        p = access.principal(request)
        if p.kind == "key":
            block["credential"] = "legacy" if p.scope == "legacy" else "scoped"
        banner = access.banner_context(request)
        block["grace_until"] = banner.get("grace_until")
        block["grace_kind"] = banner.get("grace_kind") if block["grace_until"] else None
    except Exception:
        log.debug("could not build the /ha/state auth block", exc_info=True)
    return block


@router.get("/state")
async def ha_state(request: Request):
    """Snapshot for the Home Assistant integration to poll.

    A pi_remote satellite reports only the common, device-local fields
    (display, presence, printers): its server is the fleet hub and already
    exposes expiring/counts/timers/thermometers/satellites for the whole
    kitchen, so a satellite duplicating them would just show the same numbers
    twice under two different HA devices."""
    mode = settings.deployment_mode or "server"
    payload = {
        "app": "pantryraider",
        "version": APP_VERSION,
        "mode": mode,
        "device_id": settings.device_id,
        "hostname": device_hostname(),
        "display": _display_block(),
        "presence": await _presence_block(),
        "printers": _printers_block(),
        "features": list(HA_FEATURES),
        "ha_access": _ha_access_block(),
        "auth": _auth_block(request),
    }
    if mode != "pi_remote":
        payload["expiring"] = await _expiring_block()
        payload["counts"] = _counts_block()
        payload["timers"] = _timers_block()
        payload["thermometers"] = _thermometers_block()
        payload["satellites"] = _satellites_block()
    return payload


# -- POST /ha/settings --------------------------------------------------------


class HaSettingsIn(BaseModel):
    display_idle_timeout: int | None = None
    screensaver_minutes: int | None = None
    screensaver_mode: str | None = None
    wake_on_presence: str | None = None


@router.post("/settings")
async def ha_settings(body: HaSettingsIn):
    """Apply a subset of kiosk display settings from Home Assistant.

    Only the fields present in the request are validated and applied
    (mirrors /setup/save's exclude_unset convention), so a single-field HA
    service call never touches anything else."""
    provided = body.model_dump(exclude_unset=True)
    data: dict = {}

    if "display_idle_timeout" in provided:
        v = provided["display_idle_timeout"]
        if not isinstance(v, int) or isinstance(v, bool) or not (0 <= v <= 120):
            raise HTTPException(422, detail="display_idle_timeout must be an integer between 0 and 120")
        data["display_idle_timeout"] = v

    if "screensaver_minutes" in provided:
        v = provided["screensaver_minutes"]
        if not isinstance(v, int) or isinstance(v, bool) or not (0 <= v <= 120):
            raise HTTPException(422, detail="screensaver_minutes must be an integer between 0 and 120")
        data["screensaver_minutes"] = v

    if "screensaver_mode" in provided:
        v = provided["screensaver_mode"]
        if v not in SCREENSAVER_MODES:
            raise HTTPException(
                422, detail=f"screensaver_mode must be one of {', '.join(SCREENSAVER_MODES)}")
        data["screensaver_mode"] = v

    if "wake_on_presence" in provided:
        v = provided["wake_on_presence"]
        if v not in ("auto", "on", "off"):
            raise HTTPException(422, detail="wake_on_presence must be one of auto, on, off")
        data["wake_on_presence"] = v

    if data:
        settings.save(data)
        # Mirror the display idle timeout / presence-wake mode to the host
        # bridge, the same way routers/setup/save.py does
        # (FoodAssistant-otiy). Best-effort and Pi-only; the helper itself
        # no-ops off a Pi.
        if "display_idle_timeout" in data or "wake_on_presence" in data:
            try:
                from .setup import _push_display_idle
                await _push_display_idle()
            except Exception:
                pass

    return {"ok": True, "applied": list(data.keys())}


# -- POST /ha/display ----------------------------------------------------------


class HaDisplayIn(BaseModel):
    action: str


@router.post("/display")
async def ha_display(body: HaDisplayIn):
    """Blank or wake the kiosk display on behalf of a Home Assistant
    automation, proxying to the host bridge the same way routers/setup.py's
    manual /display/blank and /display/wake buttons do.

    Off a Pi (no attached display, no bridge to ask) this returns ok:false
    with a 200, not a 404/500: the HA integration decides for itself whether
    to expose the display controls, based on the install's mode from
    GET /ha/state, but a stray call reaching the wrong install must never
    surface as a hard error."""
    if body.action not in ("sleep", "wake"):
        raise HTTPException(422, detail="action must be sleep or wake")
    if not is_raspberry_pi():
        return {"ok": False, "detail": "No attached display on this install."}
    path = "/display/blank" if body.action == "sleep" else "/display/wake"
    try:
        async with bridge_client(timeout=6.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}{path}")
        if r.status_code != 200:
            return {"ok": False, "detail": f"Bridge returned {r.status_code}."}
        body_json = r.json()
        if isinstance(body_json, dict) and "ok" in body_json:
            return body_json
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


# -- POST /ha/connect -----------------------------------------------------------


class HaConnectIn(BaseModel):
    base_url: str
    token: str
    # 'dedicated' from an integration that made a limited Home Assistant
    # account for this install; anything else (an older integration sends
    # nothing) is stored as the owner's token.
    token_kind: str = ""
    component_version: str = ""


def _after_token_change(background: BackgroundTasks) -> None:
    """Spread a new or cleared Home Assistant token: forget the last token
    check now, then refresh this device's own Stream Deck and ask every
    satellite that checks in to pull its config again. All best effort.

    The deck push and the resync run after the reply is sent. Home Assistant
    waits only ten seconds for this request, and when it gives up it removes
    the token it just handed over, which this install has already saved; a
    slow deck push must never be the reason it gives up."""
    try:
        from ..services import ha_client
        ha_client.invalidate_checks()
    except Exception:
        log.debug("could not reset the Home Assistant checks", exc_info=True)
    background.add_task(_spread_token_change)


async def _spread_token_change() -> None:
    """The slow half of _after_token_change, run after the reply."""
    try:
        from ..services import deck_config
        await deck_config.apush_to_deck()
    except Exception:
        log.debug("could not refresh the Stream Deck config", exc_info=True)
    try:
        from ..services import devices as devices_svc
        for row in devices_svc.list_devices():
            if (row.get("deployment_mode") == "pi_remote"
                    and row.get("source") == "heartbeat" and row.get("device_id")):
                devices_svc.queue_command(row["device_id"], "resync")
    except Exception:
        log.debug("could not queue satellite resyncs", exc_info=True)


@router.post("/connect")
async def ha_connect(body: HaConnectIn, background: BackgroundTasks):
    """Receive this install's own Home Assistant connection back from the HA
    integration after pairing (Dan's #7 on FoodAssistant-ju93): once HA knows
    about Pantry Raider, it can hand back a base_url + long-lived token so
    cameras and HA-sourced entities work with zero manual copy-paste of a
    token into Settings.

    The connection is saved even when the best-effort verification below
    fails. HA's internal URL is sometimes reachable from the browser that set
    up the integration but not from this container (a different Docker
    network, a `homeassistant.local` name this container's resolver does not
    have), so a failed probe is not proof the URL is wrong. Rejecting the
    save on a bad probe would leave the owner unable to complete the exact
    flow this endpoint exists for. Instead we save and report `verified` so
    the integration/component can warn the user without blocking the
    handoff."""
    base = (body.base_url or "").strip().rstrip("/")
    token = (body.token or "").strip()
    if not base or not (base.startswith("http://") or base.startswith("https://")):
        raise HTTPException(422, detail="base_url must start with http:// or https://")
    if not token:
        raise HTTPException(422, detail="token must not be empty")

    # The probe fetches a caller-supplied URL, so it must not become a blind
    # reachability oracle for internal-only services (SSRF, FoodAssistant-0h8i).
    # Home Assistant legitimately lives on the LAN, so private addresses are
    # allowed, but loopback (the app's own admin surface, the host bridge),
    # link-local (cloud metadata), and reserved ranges are refused. The guarded
    # client pins the resolved address and re-checks every redirect hop. A
    # blocked target is rejected outright rather than saved, so an unauthorized
    # caller cannot even persist an internal base_url.
    from ..services import egress
    if not egress.is_safe_public_url(base, allow_private=True):
        raise HTTPException(400, detail=egress.BLOCKED_URL_MESSAGE)

    verified = False
    try:
        async with egress.guarded_async_client(allow_private=True, timeout=4.0) as c:
            r = await c.get(f"{base}/api/", headers={"Authorization": f"Bearer {token}"})
        verified = r.status_code in (200, 201)
    except egress.BlockedHostError:
        raise HTTPException(400, detail=egress.BLOCKED_URL_MESSAGE)
    except Exception:
        verified = False

    from ..services import ha_client

    kind = "dedicated" if (body.token_kind or "").strip() == "dedicated" else "owner-legacy"
    settings.save({
        "streamdeck_ha_base_url": base,
        "streamdeck_ha_token": token,
        "ha_token_kind": kind,
        "ha_token_admin": None,
        "ha_token_expires": ha_client.token_expiry(token),
    })
    # Whether the new token is an administrator's is asked again on the next
    # check, which _after_token_change brings forward. The server's own deck
    # and every satellite still holding a copy of the token pick up the new
    # one (the old one stays valid for another hour on the Home Assistant
    # side, so nothing drops in between).
    _after_token_change(background)
    return {"ok": True, "verified": verified}


# -- POST /ha/disconnect --------------------------------------------------------


@router.post("/disconnect")
async def ha_disconnect(background: BackgroundTasks):
    """Forget the Home Assistant connection the integration handed over.

    The integration calls this when its owner turns connect-back off, just
    before it removes the tokens it made. Same callers as /ha/connect."""
    settings.save({
        "streamdeck_ha_base_url": "",
        "streamdeck_ha_token": "",
        "ha_token_kind": "",
        "ha_token_admin": None,
        "ha_token_expires": "",
    })
    _after_token_change(background)
    return {"ok": True}
