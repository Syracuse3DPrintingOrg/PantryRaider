"""Action Items (notifications) inbox API (FoodAssistant-iut3).

A small REST surface over the persistent action-items store. The Pending page
shows the active items on top of the pending scans and calls these endpoints for
the quick actions (archive, snooze 24h, resolve). Generators that raise items
(expired food, the leftovers prompt) live elsewhere and call the service
directly; this router is the read + user-action side.

On a satellite the pending list is owned by the main server, so these calls
forward upstream just like the pending endpoints, keeping one inbox of record.
"""
from __future__ import annotations

import time

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..services import action_items, upstream

router = APIRouter(prefix="/action-items", tags=["action-items"])

_fwd_client = httpx.AsyncClient(timeout=20.0)
from ..services.ttl_cache import TTLCache
_count_fwd_cache = TTLCache(5.0)


def _upstream() -> str | None:
    if settings.is_satellite() and settings.upstream_linked():
        return settings.remote_server_url.rstrip("/")
    return None


async def _forward(request: Request, subpath: str) -> Response:
    return await upstream.forward(
        _fwd_client, request, f"{_upstream()}/action-items{subpath}")


class SnoozePayload(BaseModel):
    hours: float = 24.0


# Refresh the food-expired items from Grocy at most this often, so a page that
# polls the inbox does not hammer Grocy on every request.
_REFRESH_THROTTLE_SECS = 60.0
_last_refresh = 0.0


async def _maybe_refresh(db: Session) -> None:
    """Sync food-expired items from Grocy and the unused paired key offers,
    throttled. Best-effort."""
    global _last_refresh
    now = time.monotonic()
    if now - _last_refresh < _REFRESH_THROTTLE_SECS:
        return
    _last_refresh = now
    try:
        await action_items.refresh_food_expired(db)
    except Exception:  # noqa: BLE001 - never let a generator failure break the inbox
        pass
    try:
        # An offer whose device has connected since leaves the inbox here,
        # rather than at the next hourly pass.
        await run_in_threadpool(action_items.refresh_unused_paired_keys, db)
    except Exception:  # noqa: BLE001
        pass


@router.get("")
async def list_items(request: Request, db: Session = Depends(get_db)):
    """Active action items (open + due snoozed), newest first, plus a count."""
    if _upstream():
        return await _forward(request, "")
    await _maybe_refresh(db)
    items = await run_in_threadpool(action_items.list_active, db)
    return {"items": items, "count": len(items)}


@router.get("/count")
async def count_items(request: Request, db: Session = Depends(get_db)):
    """Just the active count, for the inbox nav badge."""
    if _upstream():
        # Forwarded-count micro-cache (FoodAssistant-7dt9): the nav badge, the
        # Glance pill, and the page poller can all ask within the same seconds;
        # 5s of staleness is invisible for an inbox count and collapses the
        # burst into one upstream request.
        hit = _count_fwd_cache.get()
        if hit is not None:
            return hit
        resp = await _forward(request, "/count")
        if getattr(resp, "status_code", 500) == 200:
            _count_fwd_cache.set(resp)
        return resp
    await _maybe_refresh(db)
    return {"count": await run_in_threadpool(action_items.count_active, db)}


@router.post("/archive-all")
async def archive_all_items(request: Request, db: Session = Depends(get_db)):
    """Archive every active action item at once (FoodAssistant-n5vb)."""
    if _upstream():
        return await _forward(request, "/archive-all")
    return {"ok": True,
            "archived": await run_in_threadpool(action_items.archive_all, db)}


@router.post("/{item_id}/archive")
async def archive_item(item_id: int, request: Request, db: Session = Depends(get_db)):
    if _upstream():
        return await _forward(request, f"/{item_id}/archive")
    row = await run_in_threadpool(action_items.archive, db, item_id)
    return {"ok": row is not None, "item": row}


@router.post("/{item_id}/resolve")
async def resolve_item(item_id: int, request: Request, db: Session = Depends(get_db)):
    if _upstream():
        return await _forward(request, f"/{item_id}/resolve")
    row = await run_in_threadpool(action_items.resolve, db, item_id)
    return {"ok": row is not None, "item": row}


@router.post("/{item_id}/snooze")
async def snooze_item(item_id: int, request: Request,
                      payload: SnoozePayload = SnoozePayload(),
                      db: Session = Depends(get_db)):
    if _upstream():
        return await _forward(request, f"/{item_id}/snooze")
    row = await run_in_threadpool(action_items.snooze, db, item_id, payload.hours)
    return {"ok": row is not None, "item": row}
