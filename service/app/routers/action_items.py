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
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..services import action_items

router = APIRouter(prefix="/action-items", tags=["action-items"])

_fwd_client = httpx.AsyncClient(timeout=20.0)


def _upstream() -> str | None:
    if settings.is_satellite() and settings.remote_server_url and settings.upstream_api_key:
        return settings.remote_server_url.rstrip("/")
    return None


async def _forward(request: Request, subpath: str) -> Response:
    base = _upstream()
    headers = {"X-API-Key": settings.upstream_api_key}
    ct = request.headers.get("content-type")
    if ct:
        headers["Content-Type"] = ct
    body = await request.body()
    try:
        up = await _fwd_client.request(
            request.method, f"{base}/action-items{subpath}",
            headers=headers, params=dict(request.query_params), content=body or None,
        )
    except Exception as exc:
        return JSONResponse({"detail": f"could not reach the main server: {exc}"}, status_code=502)
    return Response(content=up.content, status_code=up.status_code,
                    media_type=up.headers.get("content-type", "application/json"))


class SnoozePayload(BaseModel):
    hours: float = 24.0


# Refresh the food-expired items from Grocy at most this often, so a page that
# polls the inbox does not hammer Grocy on every request.
_REFRESH_THROTTLE_SECS = 60.0
_last_refresh = 0.0


async def _maybe_refresh(db: Session) -> None:
    """Sync food-expired items from Grocy, throttled. Best-effort."""
    global _last_refresh
    now = time.monotonic()
    if now - _last_refresh < _REFRESH_THROTTLE_SECS:
        return
    _last_refresh = now
    try:
        await action_items.refresh_food_expired(db)
    except Exception:  # noqa: BLE001 - never let a generator failure break the inbox
        pass


@router.get("")
async def list_items(request: Request, db: Session = Depends(get_db)):
    """Active action items (open + due snoozed), newest first, plus a count."""
    if _upstream():
        return await _forward(request, "")
    await _maybe_refresh(db)
    items = action_items.list_active(db)
    return {"items": items, "count": len(items)}


@router.get("/count")
async def count_items(request: Request, db: Session = Depends(get_db)):
    """Just the active count, for the inbox badge."""
    if _upstream():
        return await _forward(request, "/count")
    return {"count": action_items.count_active(db)}


@router.post("/{item_id}/archive")
async def archive_item(item_id: int, request: Request, db: Session = Depends(get_db)):
    if _upstream():
        return await _forward(request, f"/{item_id}/archive")
    row = action_items.archive(db, item_id)
    return {"ok": row is not None, "item": row}


@router.post("/{item_id}/resolve")
async def resolve_item(item_id: int, request: Request, db: Session = Depends(get_db)):
    if _upstream():
        return await _forward(request, f"/{item_id}/resolve")
    row = action_items.resolve(db, item_id)
    return {"ok": row is not None, "item": row}


@router.post("/{item_id}/snooze")
async def snooze_item(item_id: int, request: Request,
                      payload: SnoozePayload = SnoozePayload(),
                      db: Session = Depends(get_db)):
    if _upstream():
        return await _forward(request, f"/{item_id}/snooze")
    row = action_items.snooze(db, item_id, payload.hours)
    return {"ok": row is not None, "item": row}
