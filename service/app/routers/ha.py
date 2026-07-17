"""Home Assistant integration API (FoodAssistant-ju93).

Two endpoints that let a Home Assistant custom integration mirror this
install's state and push a few kiosk display settings back, without HA having
to know about Grocy, the host bridge, or the fleet's satellite/server split
directly. Auth rides the same X-API-Key headless-client path every other
integration (satellite sync, the Stream Deck controller) already uses; the
require_auth middleware in main.py already lets a valid key through, and also
lets everything through when the install has no password set at all, so no
extra gating is needed here.

Every value in GET /ha/state degrades to a calm empty/zero on a backend
failure rather than a 500: a Home Assistant sensor reading this endpoint every
few seconds should never see the integration itself go down because Grocy, the
host bridge, or a thermometer reader happened to be unreachable this poll.
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import (
    APP_VERSION,
    SCREENSAVER_MODES,
    device_hostname,
    settings,
)
from ..hardware import is_raspberry_pi
from ..services.bridge import bridge_client

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


@router.get("/state")
async def ha_state():
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
        # bridge, the same way routers/setup.py's save path does
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


@router.post("/connect")
async def ha_connect(body: HaConnectIn):
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

    settings.save({"streamdeck_ha_base_url": base, "streamdeck_ha_token": token})
    # These fields are in SATELLITE_PULL_FIELDS, so any satellite already
    # inherits the new connection on its next sync; there is no separate push
    # to do here (unlike the AI provider settings, streamdeck_ha_* has no
    # cached client to invalidate, so reset_providers() does not apply).
    return {"ok": True, "verified": verified}
