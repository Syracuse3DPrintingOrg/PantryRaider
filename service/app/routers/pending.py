import json
from datetime import date
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models.db_models import PendingItem
from ..models.food import FoodItem, FoodCategory, StorageType
from ..services.barcode import (
    lookup_barcode, BarcodeNotFound, BarcodeServiceError, BarcodeStoreLocal,
    is_store_local_barcode,
)
from ..services.defaults import apply_defaults
from ..services.grocy import GrocyClient, stock_has_product
from ..services import (best_by_provenance, expiry_learning, scan_session,
                        scanner_mode, shopping_source)

router = APIRouter(prefix="/pending", tags=["pending"])

# Forwarding client for the satellite -> main server case (see _forward).
_fwd_client = httpx.AsyncClient(timeout=20.0)
from ..services.ttl_cache import TTLCache
_count_fwd_cache = TTLCache(5.0)


def _upstream() -> Optional[str]:
    """The main server's base URL if this device is a satellite, else None.

    Pending scans are the inventory owner's data, so they always live on the
    main server. A satellite keeps no pending rows of its own: it forwards every
    pending call to the server and shows what the server returns. That way a scan
    taken on a Pi Remote is immediately visible in the server UI and on every
    other satellite, and a single Pending list stays the source of truth.
    """
    if settings.is_satellite() and settings.remote_server_url and settings.upstream_api_key:
        return settings.remote_server_url.rstrip("/")
    return None


async def _forward(request: Request, subpath: str) -> Response:
    """Proxy this pending request to the main server, preserving method/body.

    Authenticated with the satellite's upstream API key, the same key the Grocy/
    Mealie proxy uses. Returns the server's response verbatim so the browser sees
    exactly what it would talk to the server directly.
    """
    base = _upstream()
    headers = {"X-API-Key": settings.upstream_api_key}
    ct = request.headers.get("content-type")
    if ct:
        headers["Content-Type"] = ct
    body = await request.body()
    try:
        up = await _fwd_client.request(
            request.method,
            f"{base}/pending{subpath}",
            headers=headers,
            params=dict(request.query_params),
            content=body or None,
        )
    except Exception:
        return JSONResponse(
            {"detail": "The main server is not reachable. "
                       "This will work again when it is."},
            status_code=502,
        )
    media = up.headers.get("content-type", "application/json")
    return Response(content=up.content, status_code=up.status_code, media_type=media)


async def _autocheck_shopping(item_name: str) -> None:
    """Check off any shopping-list items that token-match item_name.

    Routes through the shopping seam, so it ticks the Grocy list or the
    Mealie list, whichever this install shops from."""
    await shopping_source.autocheck(item_name)


# Longest barcode we will accept from a scanner. UPC-A is 12, EAN-13 is 13, and
# GS1 variable-weight / GS1-128 product codes run to roughly 22. Anything beyond
# this is concatenation from a scanner buffer that did not clear between scans,
# so it is refused rather than queued (FoodAssistant-doz6).
_MAX_BARCODE_LEN = 24


def gtin_check_digit_ok(code: str) -> bool:
    """True when an 8/12/13/14-digit numeric GTIN has a valid check digit.

    A misread or partial scan of a UPC-A/EAN-13 almost always fails the GS1
    check digit, so we can reject a corrupt full-length code instead of queueing
    nonsense (FoodAssistant-pmry). Non-GTIN lengths and any code with non-digits
    are NOT validated here (we cannot, so we let them through). Pure and
    unit-testable: it only does arithmetic."""
    if not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return True
    digits = [int(c) for c in code]
    body, check = digits[:-1], digits[-1]
    # GS1: from the rightmost body digit leftward, weights alternate 3, 1, 3, 1...
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - (total % 10)) % 10 == check


class ScanRequest(BaseModel):
    barcode: str
    quantity: float = 1.0
    source: str = "scanner"


class PendingUpdate(BaseModel):
    name: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    category: Optional[str] = None
    storage_type: Optional[str] = None
    best_by_date: Optional[str] = None   # "" clears the date
    brand: Optional[str] = None
    notes: Optional[str] = None


class CommitRequest(BaseModel):
    ids: Optional[list[int]] = None   # None = commit everything


def _row_dict(row: PendingItem, duplicate: bool = False) -> dict:
    return {
        "id": row.id,
        "barcode": row.barcode,
        "name": row.name,
        "quantity": row.quantity,
        "unit": row.unit,
        "category": row.category,
        "storage_type": row.storage_type,
        "best_by_date": row.best_by_date,
        "best_by_source": row.best_by_source,
        "brand": row.brand,
        "notes": row.notes,
        "lookup_failed": bool(row.lookup_failed),
        # True while the background name lookup is still running for a
        # fast-acked scan (FoodAssistant-x61t): the card shows "looking up..."
        # until it clears and the resolved name lands.
        "enriching": bool(row.enriching),
        # Derived from the barcode alone (no schema change needed): a
        # store-assigned/random-weight code can never be looked up, so the
        # pending card should prompt for a photo instead of the usual
        # "lookup failed, fix the name" hint.
        "store_local": bool(row.barcode) and is_store_local_barcode(row.barcode),
        # True when this product already has stock in Grocy. Informational only:
        # the item can still be committed, and a commit on a different day lands a
        # separate stock entry so each scan keeps its own expiration.
        "duplicate": bool(duplicate),
        "source": row.source,
        "created_at": row.created_at,
    }


async def _duplicate_names(rows: list[PendingItem]) -> set[str]:
    """Lower-cased names among ``rows`` that already have stock in Grocy.

    One Grocy stock fetch covers the whole list. Never raises: if Grocy is
    unreachable or misconfigured the duplicate hint is simply omitted (the
    pending list must still load), so an empty set is returned on any error.
    """
    wanted = {(r.name or "").strip().lower() for r in rows if (r.name or "").strip()}
    if not wanted:
        return set()
    try:
        stock = await GrocyClient().get_stock()
    except Exception:
        return set()
    return {name for name in wanted if stock_has_product(name, stock)}


async def _is_duplicate(name: str) -> bool:
    """True when a single product ``name`` already has stock in Grocy.

    Never raises: a Grocy outage just drops the hint (returns False) so a scan
    is never blocked by an inventory lookup.
    """
    if not (name or "").strip():
        return False
    try:
        return await GrocyClient().has_in_stock(name)
    except Exception:
        return False


@router.post("/scan")
async def scan_barcode(body: ScanRequest, request: Request, db: Session = Depends(get_db)):
    """Headless scanner entry point: look up the barcode and queue it as pending.

    Unknown barcodes are still queued (lookup_failed=true) so a scan never
    silently disappears: the name can be fixed on the Pending page.

    On a satellite this forwards to the main server, which owns the pending list.
    """
    if _upstream():
        return await _forward(request, "/scan")

    barcode = body.barcode.strip()
    if not barcode:
        raise HTTPException(400, "Barcode is required")
    # Reject an implausibly long code rather than queueing nonsense. A real
    # product barcode (UPC-A 12, EAN-13 13, GS1 variable-weight up to ~22) never
    # runs this long; a much longer string is concatenation from a scanner buffer
    # that did not clear between scans (an HA-side input_text.barcode_buffer that
    # never reset), so we refuse it instead of polluting the pending list. The
    # scanner UI ignores the body, so this returns a readable 200 status rather
    # than a hard error (FoodAssistant-doz6).
    if len(barcode) > _MAX_BARCODE_LEN:
        return JSONResponse(
            {"status": "rejected", "reason": "barcode too long",
             "barcode": barcode[:_MAX_BARCODE_LEN] + "…", "length": len(barcode)},
            status_code=200,
        )
    # NOTE: we deliberately do NOT reject a code whose GTIN check digit fails.
    # The headless scanner UI just POSTs and navigates, so a rejection is silent,
    # which makes scanning look completely broken when a scanner produces an
    # occasional misread. A code that fails lookup still queues as "Unknown" for
    # the user to fix on the Pending page, which is far better than a scan
    # disappearing. gtin_check_digit_ok() is kept for callers that can surface
    # the result to the user (FoodAssistant-pmry).

    # Scanner mode routes the same physical scan to a different action
    # (FoodAssistant-8jbk). "inventory" (the default) falls through to the
    # pending-queue behavior below, unchanged; the others act immediately and
    # return a short status the scanner UI / deck can show. Errors come back as
    # a 200 status object rather than an exception so a scan never hard-fails.
    mode = scanner_mode.get_mode()
    if mode == "audit":
        # Read-only stock count: record the scan against the active audit session
        # (FoodAssistant-ugku). Nothing is queued or written to Grocy. Resolve the
        # barcode to a product name so it can match the location's expected stock;
        # an unknown code is still recorded under its code so the scan is not lost.
        from ..services import audit
        if not audit.is_active():
            # No session open: begin a whole-pantry count so switching to Audit
            # (from a NeoKey or the mode picker) and scanning just starts
            # counting everything, rather than refusing the scan. A specific
            # area is still chosen on the audit page. Fail-soft: an unreachable
            # Grocy starts with an empty expected set instead of dropping the
            # scan, and the audit page fills it in once Grocy answers.
            try:
                expected = await GrocyClient().get_full_stock()
            except Exception:  # noqa: BLE001
                expected = []
            audit.start(audit.ALL_AREAS_LABEL, expected)
        name = f"Unknown ({barcode})"
        try:
            item = await lookup_barcode(barcode, db)
            name = item.name
        except (BarcodeNotFound, BarcodeServiceError):
            pass
        result = audit.record_scan(name, barcode)
        return {"mode": mode, **result}
    if mode == "consume":
        grocy = GrocyClient()
        try:
            await grocy.consume_by_barcode(barcode, body.quantity)
            return {"status": "consumed", "barcode": barcode, "mode": mode}
        except Exception as e:  # noqa: BLE001 - unknown barcode / no stock
            # Legacy stock: items imported before barcodes were registered in
            # Grocy have nothing for the by-barcode endpoint to match. Resolve
            # the code through the same lookup that named the product when it
            # was added; an exact name match identifies the product, the
            # barcode is linked for next time, and the consume retries.
            try:
                item = await lookup_barcode(barcode, db)
                pid = await grocy.product_id_by_name(item.name)
                if pid is not None:
                    await grocy.ensure_product_barcode(pid, barcode)
                    await grocy.consume_by_barcode(barcode, body.quantity)
                    return {"status": "consumed", "barcode": barcode,
                            "mode": mode, "linked": item.name}
            except Exception:  # noqa: BLE001 - fall through to the report
                pass
            # An unreachable Grocy is an outage, not an unlinked barcode: say
            # the honest reason instead of blaming the scan
            # (FoodAssistant-2cmm).
            from ..services.grocy import GrocyError
            if isinstance(e, GrocyError) and "not reachable" in str(e):
                error = str(e)
            else:
                error = ("No stocked product is linked to this barcode. "
                         "Add the item once through Manage Pantry and the "
                         "barcode links automatically. (" + str(e) + ")")
            return JSONResponse(
                {"status": "consume_failed", "barcode": barcode, "mode": mode,
                 "error": error},
                status_code=200,
            )
    if mode == "shopping":
        name = f"Unknown ({barcode})"
        try:
            item = await lookup_barcode(barcode, db)
            name = item.name
        except (BarcodeNotFound, BarcodeServiceError):
            pass
        try:
            await shopping_source.quick_add(name)
            return {"status": "shopping_added", "name": name, "mode": mode}
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"status": "shopping_failed", "name": name, "mode": mode, "error": str(e)},
                status_code=200,
            )

    # Same barcode already pending → bump quantity instead of duplicating
    existing = (
        db.query(PendingItem)
        .filter(PendingItem.barcode == barcode)
        .first()
    )
    if existing:
        existing.quantity = (existing.quantity or 1.0) + body.quantity
        db.commit()
        dup = await _is_duplicate(existing.name)
        return {"status": "merged", "item": _row_dict(existing, dup)}

    # Fast-ack (FoodAssistant-x61t): a continuous UART/keyboard-wedge scan must
    # not wait on Open Food Facts and the LLM before the user gets a response.
    # Queue the scan instantly as a placeholder with the category-rule defaults,
    # flag it enriching, and hand the name lookup to a background task that
    # updates the row when it lands. The response says "Saved, looking up..."
    # so the on-screen list can show the row immediately and fill in the name.
    placeholder = apply_defaults(FoodItem(name=f"Unknown ({barcode})"), db)
    row = PendingItem(
        barcode=barcode,
        name=placeholder.name,
        quantity=body.quantity,
        unit=placeholder.unit,
        category=placeholder.category.value,
        storage_type=placeholder.storage_type.value,
        best_by_date=placeholder.best_by_date.isoformat() if placeholder.best_by_date else None,
        best_by_source=placeholder.best_by_source,
        brand=placeholder.brand,
        lookup_failed=0,
        enriching=1,
        source=body.source,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    # The duplicate hint needs a resolved name, so it is deferred: it is filled
    # in when the Pending list next loads (GET / runs one Grocy fetch for the
    # whole list). Skipping it here keeps the ack instant and Grocy-free.
    _spawn_enrichment(row.id, barcode)
    return {
        "status": "queued",
        "enriching": True,
        "message": "Saved, looking up...",
        "item": _row_dict(row, False),
    }


async def enrich_pending_item(item_id: int, barcode: str) -> None:
    """Background: resolve ``barcode`` to a product and fill in the pending row.

    Runs after the scan was already acked (FoodAssistant-x61t). Uses its OWN
    database session (never the request session, which is closed by the time
    this runs) and never raises: any failure just leaves the row as the
    "Unknown (...)" placeholder, marked lookup_failed for the user to fix on
    the Pending page, and always clears the enriching flag.
    """
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        row = db.query(PendingItem).filter(PendingItem.id == item_id).first()
        if row is None:
            return  # deleted before enrichment finished; nothing to do
        try:
            item = await lookup_barcode(barcode, db)
            row.name = item.name
            row.unit = item.unit
            row.category = item.category.value
            row.storage_type = item.storage_type.value
            row.best_by_date = (item.best_by_date.isoformat()
                                if item.best_by_date else None)
            # Where the date came from ("llm"/"default"), kept so the commit can
            # record provenance and the label badges honestly (FoodAssistant-vb60).
            row.best_by_source = item.best_by_source
            row.brand = item.brand
            row.lookup_failed = 0
        except BarcodeStoreLocal:
            # A store-assigned/random-weight code can never be resolved: label
            # it plainly and prompt for a photo instead of guessing a product.
            row.name = f"Store barcode ({barcode}) - take a photo instead"
            row.lookup_failed = 1
        except (BarcodeNotFound, BarcodeServiceError):
            # Keep the "Unknown (barcode)" placeholder name; the user fixes it.
            row.lookup_failed = 1
        except Exception:  # noqa: BLE001 - a lookup crash must never poison the row
            row.lookup_failed = 1
        row.enriching = 0
        db.commit()
    except Exception:  # noqa: BLE001 - the background task must never crash the app
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()


def _spawn_enrichment(item_id: int, barcode: str) -> None:
    """Schedule the background name lookup for a fast-acked scan.

    Uses the running event loop's task machinery so the scan response returns
    immediately. When there is no running loop (a rare fully-synchronous call
    path) the row simply stays a placeholder until a later scan or a manual
    fix, which is better than blocking the ack. Overridable in tests."""
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    asyncio.create_task(enrich_pending_item(item_id, barcode))


class PendingItemsRequest(BaseModel):
    items: list[FoodItem]
    source: str = "photo"


@router.post("/items")
async def add_pending_items(body: PendingItemsRequest, request: Request, db: Session = Depends(get_db)):
    """Queue already-parsed items (from a photo or receipt) into the pending list.

    A receipt scan can read a dozen items at once. Persisting them here means
    they land in Pending the moment they are parsed and survive leaving the page,
    so they are reviewed, edited, and imported from the Pending list exactly like
    a barcode scan (FoodAssistant-dq4j). Items with a blank name are skipped.

    On a satellite this forwards to the main server, which owns the pending list.
    """
    if _upstream():
        return await _forward(request, "/items")

    created: list[PendingItem] = []
    for raw in body.items:
        if not (raw.name or "").strip():
            continue
        item = apply_defaults(raw, db)
        row = PendingItem(
            barcode=item.barcode,
            name=item.name,
            quantity=item.quantity or 1.0,
            unit=item.unit or "item",
            category=item.category.value,
            storage_type=item.storage_type.value,
            best_by_date=item.best_by_date.isoformat() if item.best_by_date else None,
            best_by_source=item.best_by_source,
            brand=item.brand,
            notes=item.notes,
            lookup_failed=0,
            source=body.source,
        )
        db.add(row)
        created.append(row)
    db.commit()
    for row in created:
        db.refresh(row)
    dupes = await _duplicate_names(created)
    return {
        "added": len(created),
        "items": [_row_dict(r, (r.name or "").strip().lower() in dupes) for r in created],
    }


class ScannerModePayload(BaseModel):
    mode: str = ""
    # Which surface set the mode (FoodAssistant-kh1m). A physical control
    # (source "neokey") has no screen of its own, so the app puts a toast up
    # to say what the press did. The kiosk and the Stream Deck leave this
    # empty: they already show the mode, and a toast would be noise.
    source: str = ""


@router.get("/scanner-mode")
async def scanner_mode_get(request: Request):
    """Current scanner mode. Lives on the inventory owner (the main server)."""
    if _upstream():
        return await _forward(request, "/scanner-mode")
    return scanner_mode.get_state()


@router.post("/scanner-mode")
async def scanner_mode_set(body: ScannerModePayload, request: Request):
    if _upstream():
        # The mode is the main server's authoritative state, so it forwards
        # there. But a physical control's on-screen feedback (the toast plus
        # the jump to the scan screen) is LOCAL: whoever pressed the NeoKey is
        # standing at THIS device's kiosk, not the server's. So we announce
        # here and forward the mode alone, without the source, so the server
        # updates state without also flashing its own screens.
        _announce_mode(_state_for(body.mode), body.source)
        return await _forward_scanner_mode(body.mode)
    state = scanner_mode.set_mode(body.mode)
    _announce_mode(state, body.source)
    return state


async def _forward_scanner_mode(mode: str) -> Response:
    """Forward just the mode to the main server (no source, so the server does
    not also announce a satellite's physical press on its own screens). Uses
    the same request() path the other pending forwards use."""
    base = _upstream()
    headers = {"X-API-Key": settings.upstream_api_key,
               "Content-Type": "application/json"}
    try:
        up = await _fwd_client.request(
            "POST",
            f"{base}/pending/scanner-mode",
            headers=headers,
            content=json.dumps({"mode": mode}).encode(),
        )
    except Exception:  # noqa: BLE001
        return JSONResponse(
            {"detail": "The main server is not reachable. "
                       "This will work again when it is."},
            status_code=502,
        )
    return Response(content=up.content, status_code=up.status_code,
                    media_type=up.headers.get("content-type", "application/json"))


# Physical controls that deserve on-screen feedback when they change the mode:
# they have no display of their own, so without this a press is invisible to
# anyone standing at the kiosk.
_PHYSICAL_MODE_SOURCES = ("neokey",)

# Where each mode's scan happens, so a physical mode change lands the kiosk on
# the screen the next scan flows through. Every mode, Audit included, scans
# from Manage: a physical key switches the mode in place without sending the
# person off to a different page (the dedicated /ui/audit page is still there
# for picking an area, but a key press stays put).
_MODE_SCREENS = {
    "inventory": "ui/add",
    "consume": "ui/add",
    "shopping": "ui/add",
    "audit": "ui/add",
}


def _state_for(mode: str) -> dict:
    """A {mode, label} view for a mode name, without touching stored state (a
    satellite never stores the mode locally; the server owns it)."""
    mode = str(mode or "").strip().lower()
    return {"mode": mode,
            "label": scanner_mode.MODE_LABELS.get(mode, mode.title())}


def _announce_mode(state: dict, source: str) -> None:
    """On-screen feedback when a physical control selected the mode: a toast
    naming the new mode, and a jump to that mode's scan screen so the kiosk
    lands where the next scan goes.

    Best-effort: a mode change must never fail because the feedback channel is
    unhappy, and an unknown source stays quiet rather than guessing."""
    if str(source or "").strip().lower() not in _PHYSICAL_MODE_SOURCES:
        return
    try:
        from ..services import ha_events
        mode = str(state.get("mode") or "")
        ha_events.add_confirmation(
            f"Scanner mode: {state.get('label') or mode}",
            title="Barcode scanner")
        screen = _MODE_SCREENS.get(mode)
        if screen:
            # always=True: a physical press is local feedback, so the kiosk
            # follows it even where on-screen HA events are turned off.
            ha_events.add_navigate(screen, always=True)
    except Exception:  # noqa: BLE001
        pass


@router.post("/scanner-mode/cycle")
async def scanner_mode_cycle(request: Request):
    """Advance to the next scanner mode (the Stream Deck key calls this)."""
    if _upstream():
        return await _forward(request, "/scanner-mode/cycle")
    return scanner_mode.cycle_mode()


@router.post("/scan-session/ping")
async def scan_session_ping():
    """Heartbeat from an open scan page (FoodAssistant-x61t).

    The Manage Pantry / scan page calls this on mount and every ~10s while it
    is open. It keeps the scan session "active" for a short TTL, which is what
    the host UART reader watches (GET /gadgets/config -> scanner_uart.scan_active)
    to know when to open the serial port and read continuously. When the page
    is closed the pings stop and the session expires on its own.

    Deliberately NOT forwarded on a satellite: a UART scanner is wired to one
    device, so whether a scan page is open here is this device's own state
    (unlike the scanner MODE, which is fleet-wide and does forward)."""
    return scan_session.ping()


@router.get("/scan-session")
async def scan_session_state():
    """The current scan session: {active, expires_in}. Local to this device."""
    return scan_session.state()


@router.get("/")
async def list_pending(request: Request, db: Session = Depends(get_db)):
    if _upstream():
        return await _forward(request, "/")
    rows = db.query(PendingItem).order_by(PendingItem.created_at.desc()).all()
    dupes = await _duplicate_names(rows)
    return {"items": [_row_dict(r, (r.name or "").strip().lower() in dupes) for r in rows]}


@router.get("/count")
async def pending_count(request: Request, db: Session = Depends(get_db)):
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
    return {"count": db.query(PendingItem).count()}


@router.patch("/{item_id}")
async def update_pending(item_id: int, body: PendingUpdate, request: Request, db: Session = Depends(get_db)):
    if _upstream():
        return await _forward(request, f"/{item_id}")
    row = db.query(PendingItem).filter(PendingItem.id == item_id).first()
    if not row:
        raise HTTPException(404, "Pending item not found")
    data = body.model_dump(exclude_none=True)
    if "best_by_date" in data and data["best_by_date"] == "":
        data["best_by_date"] = None
    if "best_by_date" in data and row.suggested_source is None:
        # First time the user touches the date on this row: stash what the app
        # had suggested, so the commit can compare the user's final choice
        # against it (community shelf-life learning, FoodAssistant-ezkh).
        # "none" marks a row that had no suggested date to begin with.
        row.suggested_best_by = row.best_by_date
        row.suggested_source = (row.best_by_source or "none") if row.best_by_date else "none"
    for field, value in data.items():
        setattr(row, field, value)
    if "best_by_date" in data:
        # The user set (or cleared) the date on the review screen: it is now a
        # manual date, so any earlier "default"/"llm" origin no longer applies
        # and must not be recorded or badged (FoodAssistant-vb60).
        row.best_by_source = None
    if body.name:
        row.lookup_failed = 0
    db.commit()
    return _row_dict(row)


@router.delete("/{item_id}")
async def delete_pending(item_id: int, request: Request, db: Session = Depends(get_db)):
    if _upstream():
        return await _forward(request, f"/{item_id}")
    row = db.query(PendingItem).filter(PendingItem.id == item_id).first()
    if not row:
        raise HTTPException(404, "Pending item not found")
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


def _capture_learning(row: PendingItem) -> bool:
    """Queue one anonymous shelf-life data point from a committed row.

    Only rows where the user actually set or corrected the date qualify: the
    PATCH handler stashed the app's original suggestion in suggested_source /
    suggested_best_by, and best_by_source is still None (a manual date).
    Gated on the share_expiry_learning opt-in inside expiry_learning.record,
    which captures nothing when sharing is off. Never raises."""
    if (row.suggested_source is None or not row.best_by_date
            or row.best_by_source is not None):
        return False
    try:
        chosen = date.fromisoformat(row.best_by_date)
    except (TypeError, ValueError):
        return False
    suggested = None
    if row.suggested_best_by:
        try:
            suggested = date.fromisoformat(row.suggested_best_by)
        except (TypeError, ValueError):
            suggested = None
    # Day the item entered the pantry, so the shared value is a true
    # shelf-life-in-days even when the item waited in the review queue.
    base = date.today()
    if row.created_at:
        try:
            base = date.fromisoformat(str(row.created_at)[:10])
        except ValueError:
            pass
    return expiry_learning.record(row.name, row.barcode, row.storage_type,
                                  chosen, suggested, row.suggested_source, base)


@router.post("/commit")
async def commit_pending(body: CommitRequest, request: Request, db: Session = Depends(get_db)):
    """Import pending items into Grocy; successfully imported rows are removed.

    On a satellite this forwards to the main server, which holds the pending rows
    and imports them into its own Grocy directly (no double proxy hop).
    """
    if _upstream():
        return await _forward(request, "/commit")
    q = db.query(PendingItem)
    if body.ids:
        q = q.filter(PendingItem.id.in_(body.ids))
    rows = q.all()
    if not rows:
        return {"imported": 0, "results": []}

    grocy = GrocyClient()
    results = []
    captured = 0
    for row in rows:
        row_id = row.id
        try:
            item = FoodItem(
                name=row.name,
                quantity=row.quantity or 1.0,
                unit=row.unit or "item",
                category=FoodCategory(row.category) if row.category in FoodCategory._value2member_map_ else FoodCategory.other,
                storage_type=StorageType(row.storage_type) if row.storage_type in StorageType._value2member_map_ else StorageType.refrigerated,
                best_by_date=date.fromisoformat(row.best_by_date) if row.best_by_date else None,
                brand=row.brand,
                notes=row.notes,
                best_by_source=row.best_by_source,
            )
            item = apply_defaults(item, db)
            result = await grocy.import_item(item)
            # Community shelf-life learning (FoodAssistant-ezkh): a committed
            # row whose date the user set or corrected is one anonymous data
            # point, queued only when the sharing opt-in is on. Read before
            # the row is deleted; never blocks a commit.
            if _capture_learning(row):
                captured += 1
            db.delete(row)
            db.commit()
            results.append({"id": row_id, "status": "ok", **result})
            # Record how the best-by date was worked out, now that the item
            # has a Grocy product id, exactly like /inventory/import does
            # (FoodAssistant-vb60). best_by_provenance quietly no-ops for
            # "manual" (or unset), the no-badge default anyway.
            if item.best_by_date is not None:
                best_by_provenance.record(
                    result.get("product_id"), item.name,
                    item.best_by_source or "manual",
                    item.best_by_date.isoformat(),
                )
            if settings.barcode_autocheck_shopping and shopping_source.shopping_available():
                try:
                    await _autocheck_shopping(item.name)
                except Exception:
                    pass  # never block a commit over a shopping-list failure
        except Exception as e:
            db.rollback()
            results.append({"id": row_id, "status": "error", "error": str(e)})

    if captured:
        # Fire-and-forget upload of the freshly queued points. Failures leave
        # the queue intact for the periodic pass; the commit never waits.
        import asyncio
        try:
            asyncio.create_task(expiry_learning.flush())
        except RuntimeError:
            pass  # no running loop (sync test harness): the periodic pass sends it

    return {
        "imported": len([r for r in results if r["status"] == "ok"]),
        "results": results,
    }
