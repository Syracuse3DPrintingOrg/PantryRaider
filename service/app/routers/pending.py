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
from ..services.barcode import lookup_barcode, BarcodeNotFound, BarcodeServiceError
from ..services.defaults import apply_defaults
from ..services.grocy import GrocyClient, stock_has_product
from ..services import scanner_mode

router = APIRouter(prefix="/pending", tags=["pending"])

# Forwarding client for the satellite -> main server case (see _forward).
_fwd_client = httpx.AsyncClient(timeout=20.0)


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
    """Check off any Mealie shopping-list items that token-match item_name."""
    from ..services.mealie import MealieClient, _tokens
    mealie = MealieClient()
    item_toks = _tokens(item_name)
    if not item_toks:
        return
    lists = await mealie.get_shopping_lists()
    for lst in lists:
        detail = await mealie.get_shopping_list(lst["id"])
        for si in detail.get("listItems", []):
            if si.get("checked"):
                continue
            si_toks = _tokens(si.get("note") or "")
            if item_toks & si_toks:
                updated = {**si, "checked": True}
                await mealie.update_shopping_item(si["id"], updated)


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
        "brand": row.brand,
        "notes": row.notes,
        "lookup_failed": bool(row.lookup_failed),
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
            return JSONResponse(
                {"status": "no_audit_session", "barcode": barcode, "mode": mode,
                 "error": "Start an audit at a location first."},
                status_code=200,
            )
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
            from ..services.mealie import MealieClient
            m = MealieClient()
            lists = await m.get_shopping_lists()
            if not lists:
                return JSONResponse(
                    {"status": "shopping_failed", "mode": mode, "error": "No shopping list in Mealie."},
                    status_code=200,
                )
            await m.add_shopping_item(lists[0]["id"], name)
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

    lookup_failed = False
    try:
        item = await lookup_barcode(barcode, db)
    except (BarcodeNotFound, BarcodeServiceError):
        lookup_failed = True
        item = apply_defaults(FoodItem(name=f"Unknown ({barcode})"), db)

    row = PendingItem(
        barcode=barcode,
        name=item.name,
        quantity=body.quantity,
        unit=item.unit,
        category=item.category.value,
        storage_type=item.storage_type.value,
        best_by_date=item.best_by_date.isoformat() if item.best_by_date else None,
        brand=item.brand,
        lookup_failed=1 if lookup_failed else 0,
        source=body.source,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    dup = await _is_duplicate(row.name)
    return {"status": "queued", "item": _row_dict(row, dup)}


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


@router.get("/scanner-mode")
async def scanner_mode_get(request: Request):
    """Current scanner mode. Lives on the inventory owner (the main server)."""
    if _upstream():
        return await _forward(request, "/scanner-mode")
    return scanner_mode.get_state()


@router.post("/scanner-mode")
async def scanner_mode_set(body: ScannerModePayload, request: Request):
    if _upstream():
        return await _forward(request, "/scanner-mode")
    return scanner_mode.set_mode(body.mode)


@router.post("/scanner-mode/cycle")
async def scanner_mode_cycle(request: Request):
    """Advance to the next scanner mode (the Stream Deck key calls this)."""
    if _upstream():
        return await _forward(request, "/scanner-mode/cycle")
    return scanner_mode.cycle_mode()


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
        return await _forward(request, "/count")
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
    for field, value in data.items():
        setattr(row, field, value)
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
            )
            item = apply_defaults(item, db)
            result = await grocy.import_item(item)
            db.delete(row)
            db.commit()
            results.append({"id": row_id, "status": "ok", **result})
            if settings.barcode_autocheck_shopping and settings.mealie_configured():
                try:
                    await _autocheck_shopping(item.name)
                except Exception:
                    pass  # never block a commit over a shopping-list failure
        except Exception as e:
            db.rollback()
            results.append({"id": row_id, "status": "error", "error": str(e)})

    return {
        "imported": len([r for r in results if r["status"] == "ok"]),
        "results": results,
    }
