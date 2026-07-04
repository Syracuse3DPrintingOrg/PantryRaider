"""Pantry audit endpoints (FoodAssistant-ugku).

A read-only, location-scoped stock count. The user locks audit mode to one
storage location and scans the items there; each scan is recorded as "seen"
against the active session and compared to the location's Grocy stock, but
nothing is written to Grocy. The kiosk page (/ui/audit) polls /audit/status to
show expected vs scanned so discrepancies (missing, unexpected) stand out.

On a satellite these forward to the main server (the inventory owner), the same
way the pending router does, so a scan taken on a Pi Remote audits the server's
stock and every surface sees the same session.
"""
from __future__ import annotations

from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal
from ..services import audit
from ..services.barcode import lookup_barcode, BarcodeNotFound, BarcodeServiceError
from ..services.grocy import GrocyClient, GrocyError

router = APIRouter(prefix="/audit", tags=["audit"])

_fwd_client = httpx.AsyncClient(timeout=20.0)


def _upstream() -> Optional[str]:
    """The main server's base URL if this device is a satellite, else None."""
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
            request.method,
            f"{base}/audit{subpath}",
            headers=headers,
            params=dict(request.query_params),
            content=body or None,
        )
    except Exception:  # noqa: BLE001
        return JSONResponse(
            {"detail": "The main server is not reachable. "
                       "This will work again when it is."},
            status_code=502,
        )
    media = up.headers.get("content-type", "application/json")
    return Response(content=up.content, status_code=up.status_code, media_type=media)


class StartRequest(BaseModel):
    location: str


class AuditScanRequest(BaseModel):
    barcode: str = ""
    name: str = ""


async def _stock_for_location(location: str) -> list[dict]:
    """The Grocy stock entries whose location (or storage bucket) matches.

    Matched against both the Grocy location name and the app's storage bucket so
    either label the user picked from the location list resolves to its items."""
    entries = await GrocyClient().get_full_stock()
    loc = (location or "").strip().casefold()
    return [
        e for e in entries
        if (e.get("location_name") or "").strip().casefold() == loc
        or (e.get("storage_bucket") or "").strip().casefold() == loc
    ]


@router.get("/locations")
async def audit_locations(request: Request):
    """The storage locations that currently hold stock, for the start picker."""
    if _upstream():
        return await _forward(request, "/locations")
    try:
        entries = await GrocyClient().get_full_stock()
    except GrocyError as e:
        raise HTTPException(502, str(e))
    seen: dict[str, int] = {}
    for e in entries:
        name = (e.get("location_name") or e.get("storage_bucket") or "").strip()
        if not name:
            continue
        seen[name] = seen.get(name, 0) + 1
    locations = [{"name": n, "item_count": c} for n, c in sorted(seen.items())]
    return {"locations": locations}


@router.post("/start")
async def audit_start(body: StartRequest, request: Request):
    """Begin an audit locked to a location, snapshotting its expected stock."""
    if _upstream():
        return await _forward(request, "/start")
    location = body.location.strip()
    if not location:
        raise HTTPException(400, "A storage location is required")
    try:
        expected = await _stock_for_location(location)
    except GrocyError as e:
        raise HTTPException(502, str(e))
    return audit.start(location, expected)


@router.post("/scan")
async def audit_scan(body: AuditScanRequest, request: Request):
    """Record a scan against the active audit session (read-only, no Grocy write).

    Resolves a barcode to a product name via the same lookup the pending queue
    uses; an unknown barcode is still recorded under its code so the scan is not
    lost. Returns a 200 status object even on a failure so a headless scan never
    hard-fails."""
    if _upstream():
        return await _forward(request, "/scan")
    if not audit.is_active():
        return JSONResponse(
            {"status": "no_session", "error": "Start an audit at a location first."},
            status_code=200,
        )
    name = (body.name or "").strip()
    barcode = (body.barcode or "").strip()
    if not name and barcode:
        # SessionLocal directly: lookup_barcode caches resolved products in the
        # local DB, but an audit never mutates Grocy, so a short-lived read
        # session is all we need here.
        db: Session = SessionLocal()
        try:
            item = await lookup_barcode(barcode, db)
            name = item.name
        except (BarcodeNotFound, BarcodeServiceError):
            name = f"Unknown ({barcode})"
        finally:
            db.close()
    if not name:
        raise HTTPException(400, "A barcode or name is required")
    return audit.record_scan(name, barcode or None)


@router.get("/status")
async def audit_status(request: Request):
    """Current audit picture: expected vs scanned, with missing/unexpected."""
    if _upstream():
        return await _forward(request, "/status")
    return audit.status()


@router.post("/stop")
async def audit_stop(request: Request):
    """End the audit session and return the final snapshot."""
    if _upstream():
        return await _forward(request, "/stop")
    return audit.stop()
