"""Consolidated kiosk status poll (FoodAssistant-us1i).

The kiosk display used to poll about eight endpoints on 2-3 second intervals
(timers, HA events, the pending and action-item badge counts, scanner mode, the
presence/activity readout, system health, and the one-shot kiosk hand-off and
touch-calibration flags), roughly a hundred requests a minute on an idle panel.
This is the single GET that gathers all of it in one response, so the kiosk
makes one request per tick instead of one per field.

Contract, unchanged from the individual endpoints it stands in for:

* Same sources per field. Timers, events, scanner mode read the same shared
  atomic state files; the counts run the same DB queries and Grocy pull; the
  activity/health/hardware fields come from the same host bridge; the one-shot
  flags read the same files. Nothing here changes WHERE a datum comes from.
* Same auth posture per field. The device-telemetry fields (activity, health,
  the kiosk one-shot flags) live under ``/setup`` today and are admin-gated, so
  they are included ONLY for a caller who would clear that gate (loopback, an
  API key, or an admin session), exactly as ``_caller_is_admin`` mirrors the
  auth middleware. A remote viewer session gets them omitted, which lands it in
  the same place its individual polls do today (a 403, nothing shown). The
  viewer-legitimate fields (timers, events, counts, scanner mode) are always
  returned, matching those endpoints being reachable by a viewer.
* Same satellite routing per field. On a pi_remote the fleet-owned fields
  (timers, the pending/action/expiring counts, scanner mode) are forwarded to
  the main server in one round trip; the device-local fields (its own events
  ring and alert count, its bridge activity/health, its own flag files) are
  answered here. A field that could not be gathered is OMITTED, so the client
  leaves its last state rather than blanking when the main server blips.

The individual endpoints stay for backward compatibility; this only collapses
the kiosk's own polling onto one request.
"""
from __future__ import annotations

import json
import secrets
import time

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..services import ha_events, scanner_mode, timers
from ..services.ttl_cache import TTLCache

router = APIRouter(prefix="/kiosk", tags=["kiosk"])

# The bridge-backed health snapshot is the one merged field that is neither
# already cheap nor already self-cached: the individual /setup/system/health
# hits the host bridge on every call. At the consolidated poll's fast cadence
# that would be about thirty times the bridge traffic, so cache it here at the
# bridge's own ~60s monitoring rhythm (its warning-to-inbox sync is throttled to
# 60s anyway, so caching the result never starves it). Activity is deliberately
# NOT cached: the presence indicator needs it fresh at 2s, exactly as its own
# poll fetched it. Per-worker like the sibling count caches; N workers each hold
# their own, which only means the bridge is asked at most once per TTL per
# worker, the same shape as the existing expiring/count cache.
# Match the old dedicated /setup/system/health poll's 60s cadence, so folding
# it into the faster consolidated poll does not probe the host bridge (and run
# system_health()'s throttled warning-sync side effect) more often than before.
_health_cache = TTLCache(60.0)


def _json_of(result):
    """Normalize a per-endpoint handler's return to a dict. On the server path
    the handlers return a dict; on a satellite they return a forwarded Response
    (bytes), so parse it, or None on a non-200 (a down main server). This lets
    the satellite path reuse the EXISTING per-endpoint forwards (timers, counts,
    scanner mode), which carry their own upstream micro-caches and exist on any
    main-server version, instead of a single new upstream endpoint an older
    server would 404."""
    if isinstance(result, dict):
        return result
    if isinstance(result, Response):
        if getattr(result, "status_code", 200) != 200:
            return None
        body = getattr(result, "body", None)
        if isinstance(body, (bytes, bytearray)):
            try:
                return json.loads(body)
            except Exception:
                return None
    return None


def _caller_is_admin(request: Request) -> bool:
    """Whether this caller would clear the ``/setup`` admin gate, mirroring
    ``main.require_auth`` exactly so the consolidated endpoint never widens what
    a remote or unauthenticated client can read. The device-telemetry fields
    live under ``/setup`` today; including them only for an admin-clearing caller
    keeps a viewer session in the same place it is now (those fields omitted)."""
    # Auth disabled: the middleware is a no-op and /setup is reachable by all
    # today, so the admin fields are too. This matches current behavior on a
    # passwordless install exactly.
    if not settings.auth_password:
        return True
    if request.client and request.client.host in ("127.0.0.1", "::1"):
        return True
    sent = request.headers.get("X-API-Key", "")
    valid = settings.valid_api_keys()
    if sent and valid and any(secrets.compare_digest(sent, k) for k in valid):
        return True
    if request.session.get("authed") and not request.session.get("totp_pending"):
        if request.session.get("role") != "viewer":
            return True
    return False


def _upstream() -> str | None:
    """The main server base URL when this device is a satellite, else None,
    the same predicate the per-endpoint forwards use."""
    if settings.is_satellite() and settings.remote_server_url and settings.upstream_api_key:
        return settings.remote_server_url.rstrip("/")
    return None


async def _device_fields(out: dict, admin: bool, kiosk: bool) -> None:
    """Overlay the device-local telemetry fields, admin-gated and answered from
    this device's own host bridge and flag files. Shared by the server and
    satellite paths: presence/activity/health/hardware are local hardware on
    whatever device is showing the glass, never forwarded (recon section D)."""
    if not admin:
        return
    from ..routers import setup as setup_router
    try:
        out["activity"] = await setup_router.kiosk_activity_state()
    except Exception:
        pass
    try:
        health = _health_cache.get()
        if health is None:
            health = await setup_router.system_health()
            _health_cache.set(health)
        out["health"] = health
    except Exception:
        pass
    try:
        out["calibrate_pending"] = bool(
            (await setup_router.calibrate_touch_pending()).get("pending"))
    except Exception:
        pass
    # The kiosk hand-off flag is one-shot (read clears it), so only a kiosk
    # caller may consume it: a plain admin browser polling status must never eat
    # the display's hand-off. Today only the kiosk polls navigate/pending, so
    # gating on the kiosk flag preserves that, and folding the two old pollers
    # (base.html + setup.html) into one per page actually removes the latent
    # double-consume race the recon flagged.
    if kiosk:
        try:
            out["nav_pending"] = bool(
                (await setup_router.kiosk_navigate_pending()).get("pending"))
        except Exception:
            pass


async def _gather_local(request: Request, since: int, want_expiring: bool,
                        admin: bool, kiosk: bool, db: Session) -> dict:
    """Server / pi_hosted: every field is answered locally, from the same source
    the matching individual endpoint uses."""
    from ..routers import pending as pending_router
    from ..routers import action_items as ai_router
    from ..routers import expiring as expiring_router

    out: dict = {"server_time_epoch": time.time()}

    try:
        out["timers"] = {"timers": timers.list_timers()}
    except Exception:
        pass
    try:
        out["events"] = ha_events.poll(since)
    except Exception:
        pass

    counts: dict = {}
    try:
        counts["pending"] = int((await pending_router.pending_count(request, db)).get("count", 0))
    except Exception:
        pass
    try:
        counts["actions"] = int((await ai_router.count_items(request, db)).get("count", 0))
    except Exception:
        pass
    try:
        counts["alerts"] = ha_events.active_count()
    except Exception:
        pass
    # Expiring is the one count that pulls Grocy, so it is opt-in: only the Start
    # page asks for it. base.html never sends expiring=1, so its kiosks never
    # trigger the pull. Cached ~30s in the expiring router either way.
    if want_expiring:
        try:
            counts["expiring"] = int((await expiring_router.get_expiring_count(days=7)).get("count", 0))
        except Exception:
            pass
    if counts:
        out["counts"] = counts

    try:
        out["scanner_mode"] = scanner_mode.get_state()
    except Exception:
        pass

    await _device_fields(out, admin, kiosk)
    return out


async def _gather_satellite(request: Request, since: int, want_expiring: bool,
                            admin: bool, kiosk: bool, db: Session) -> dict:
    """pi_remote: the fleet-owned fields (timers, the pending/action counts,
    scanner mode) are answered by the EXISTING per-endpoint handlers, which
    forward upstream with their own micro-caches and exist on any main-server
    version (so a satellite on new code keeps working against an older server).
    The device-local fields (this device's own events ring and alert count, its
    bridge activity/health, its own flag files) are answered here. A field that
    could not be gathered is omitted, so the client keeps its last on-glass
    state rather than blanking when the main server blips.

    The one-shot hand-off flag is never forwarded: it is local to whichever
    device shows the glass, so the server's flag is never the satellite's to
    consume."""
    from ..routers import pending as pending_router
    from ..routers import action_items as ai_router
    from ..routers import current_recipe as cr_router
    from ..routers import expiring as expiring_router

    out: dict = {"server_time_epoch": time.time()}

    # Fleet-owned, via the existing forwards (each returns a Response upstream).
    try:
        t = _json_of(await cr_router.get_timers(request))
        if isinstance(t, dict):
            out["timers"] = t
    except Exception:
        pass
    try:
        sm = _json_of(await pending_router.scanner_mode_get(request))
        if isinstance(sm, dict):
            out["scanner_mode"] = sm
    except Exception:
        pass

    counts: dict = {}
    try:
        pc = _json_of(await pending_router.pending_count(request, db))
        if isinstance(pc, dict) and "count" in pc:
            counts["pending"] = pc["count"]
    except Exception:
        pass
    try:
        ac = _json_of(await ai_router.count_items(request, db))
        if isinstance(ac, dict) and "count" in ac:
            counts["actions"] = ac["count"]
    except Exception:
        pass
    # Expiring on a satellite dials the pulled Grocy directly (local handler,
    # 30s cache), the same source the individual /expiring/count poll uses.
    if want_expiring:
        try:
            ex = _json_of(await expiring_router.get_expiring_count(days=7))
            if isinstance(ex, dict) and "count" in ex:
                counts["expiring"] = ex["count"]
        except Exception:
            pass
    # Alerts is the satellite's OWN on-screen events count (HA posts to whichever
    # instance has the display), never the server's, so it is answered locally.
    try:
        counts["alerts"] = ha_events.active_count()
    except Exception:
        pass
    if counts:
        out["counts"] = counts

    # The satellite's own events ring, never forwarded.
    try:
        out["events"] = ha_events.poll(since)
    except Exception:
        pass

    await _device_fields(out, admin, kiosk)
    return out


@router.get("/status")
async def kiosk_status(
    request: Request,
    since: int = 0,
    kiosk: int = 0,
    expiring: int = 0,
    db: Session = Depends(get_db),
):
    """The one poll the kiosk runs. ``since`` carries the HA-events cursor (the
    client primes it high on first load so no backlog replays); ``kiosk=1`` marks
    a kiosk-latched display so the one-shot hand-off flag may be consumed;
    ``expiring=1`` opts into the Grocy-backed expiring count (Start page only)."""
    admin = _caller_is_admin(request)
    if _upstream():
        return await _gather_satellite(
            request, since, bool(expiring), admin, bool(kiosk), db)
    return await _gather_local(
        request, since, bool(expiring), admin, bool(kiosk), db)
