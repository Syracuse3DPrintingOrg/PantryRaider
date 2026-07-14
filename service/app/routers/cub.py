"""Bandit Cub endpoints (docs/design/bandit-cub.md).

GET /cub/summary is the one URL a Cub polls: the server decides what the
device should show (view takeovers, idle rotation) and hands back every block
plus the effective content settings, so a policy change never needs a
reflash. Auth rides the same X-API-Key headless-client path /ha/state uses
(the require_auth middleware in main.py; /cub is deliberately in no bypass
list). Every block degrades to a calm empty/zero on a backend failure rather
than a 500, per the /ha/state precedent: a display polling every 15 seconds
must never see the endpoint itself go down because Grocy did.

The poll doubles as the device's heartbeat: the X-Cub-* headers upsert the
registry row (services/cub.py). No headers, no row, so curl testing works.
The remaining routes back the Bandit Cubs section of the Devices pane.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..config import settings
from ..services import cub as cub_svc

router = APIRouter(prefix="/cub", tags=["cub"])


# -- GET /cub/summary ----------------------------------------------------------


async def _expiring_items() -> tuple[list[dict] | None, bool]:
    """The expiring items list, riding the same /expiring/count cache
    /ha/state uses so Cub polls never add their own Grocy pull. (None, False)
    on any failure."""
    from . import expiring as expiring_router
    from ..services.grocy import GrocyClient

    try:
        all_items = expiring_router._count_items_cache.get()
        if all_items is None:
            grocy = GrocyClient()
            all_items = await grocy.get_expiring(days=30)
            expiring_router._count_items_cache.set(all_items)
        return all_items, True
    except Exception:
        return None, False


def _timers() -> list[dict]:
    from ..services import timers as timers_svc
    try:
        return timers_svc.list_timers()
    except Exception:
        return []


def _gadget_devices() -> list[dict]:
    from ..services import gadgets
    try:
        state = gadgets.get_state()
        devices = state.get("devices", []) if isinstance(state, dict) else []
        return devices if isinstance(devices, list) else []
    except Exception:
        return []


def _hygro_devices() -> list[dict]:
    from ..services import gadgets
    try:
        state = gadgets.get_state()
        devices = state.get("hygrometers", []) if isinstance(state, dict) else []
        return devices if isinstance(devices, list) else []
    except Exception:
        return []


def _alarms() -> list[dict]:
    """The live protection alarms (fridge/freezer thresholds, doors left
    open; FoodAssistant-5c61), degrading to an empty list on any trouble."""
    from ..services import gadgets
    try:
        alarms = gadgets.active_alarms()
        return alarms if isinstance(alarms, list) else []
    except Exception:
        return []


def _counts() -> dict:
    # Same pending/action-items counts the HA snapshot reports; the builder
    # already degrades to zeros on any DB trouble.
    from .ha import _counts_block
    try:
        return _counts_block()
    except Exception:
        return {"pending": 0, "action_items": 0}


@router.get("/summary")
async def cub_summary(request: Request):
    """Everything a Cub shows, in one poll. See the design doc's contract."""
    cub_id = (request.headers.get("X-Cub-Id") or "").strip()
    overrides: dict = {}
    if cub_id:
        try:
            client_ip = request.client.host if request.client else None
            overrides = cub_svc.record_cub_heartbeat(
                cub_id,
                name=(request.headers.get("X-Cub-Name") or "").strip() or None,
                hardware_profile=(request.headers.get("X-Cub-Profile") or "").strip() or None,
                firmware_version=(request.headers.get("X-Cub-Version") or "").strip() or None,
                ip=client_ip,
            )
        except Exception:
            overrides = {}  # a registry hiccup never blocks the poll

    merged = cub_svc.merge_cub_settings(cub_svc.global_cub_settings(settings), overrides)

    timers = cub_svc.timers_block(_timers())
    probes = cub_svc.probes_block(_gadget_devices())
    alerts = cub_svc.alerts_block(_alarms())
    items, ok = await _expiring_items()
    window_days = settings.expiring_soon_days

    return {
        "v": 1,
        "generated": int(time.time()),
        "view": cub_svc.decide_view(timers, probes, merged, alerts),
        "rotation": cub_svc.rotation_blocks(merged),
        # Active protection alarms (FoodAssistant-5c61): additive, so older
        # firmware simply ignores it; [] when nothing is alarming. The
        # "alert" view this block backs ships in a later firmware release.
        "alerts": alerts,
        "expiring": cub_svc.expiring_block(items, window_days, ok=ok),
        "counts": _counts(),
        "timers": timers,
        "probes": probes,
        # Fridge/room hygrometers (FoodAssistant-q97i): additive, so older
        # firmware simply ignores it; [] whenever none are configured.
        "hygrometers": cub_svc.hygrometers_block(_hygro_devices()),
        "settings": cub_svc.settings_block(
            merged,
            units=settings.streamdeck_weather_units,
            clock_24h=settings.clock_format == "24",
        ),
    }


# -- registry management (Devices pane) -----------------------------------------


class CubEditBody(BaseModel):
    name: str | None = None
    overrides: dict | None = None


@router.get("/devices")
def list_cub_devices():
    return {"devices": cub_svc.list_cubs()}


@router.post("/devices/{device_id}")
def edit_cub_device(device_id: str, body: CubEditBody):
    """Rename a Cub and/or replace its per-device overrides. Only the fields
    present in the request are applied, so a rename never touches overrides
    and vice versa."""
    provided = body.model_dump(exclude_unset=True)
    ok = True
    if "name" in provided:
        ok = cub_svc.rename_cub(device_id, provided["name"] or "") and ok
    if "overrides" in provided:
        ok = cub_svc.set_cub_overrides(device_id, provided["overrides"] or {}) and ok
    if not ok:
        return {"ok": False, "error": "Unknown device."}
    return {"ok": True}


@router.delete("/devices/{device_id}")
def delete_cub_device(device_id: str):
    if not cub_svc.forget_cub(device_id):
        return {"ok": False, "error": "Unknown device."}
    return {"ok": True}
