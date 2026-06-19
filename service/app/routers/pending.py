from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models.db_models import PendingItem
from ..models.food import FoodItem, FoodCategory, StorageType
from ..services.barcode import lookup_barcode, BarcodeNotFound, BarcodeServiceError
from ..services.defaults import apply_defaults
from ..services.grocy import GrocyClient

router = APIRouter(prefix="/pending", tags=["pending"])


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


def _row_dict(row: PendingItem) -> dict:
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
        "source": row.source,
        "created_at": row.created_at,
    }


@router.post("/scan")
async def scan_barcode(body: ScanRequest, db: Session = Depends(get_db)):
    """Headless scanner entry point: look up the barcode and queue it as pending.

    Unknown barcodes are still queued (lookup_failed=true) so a scan never
    silently disappears: the name can be fixed on the Pending page.
    """
    barcode = body.barcode.strip()
    if not barcode:
        raise HTTPException(400, "Barcode is required")

    # Same barcode already pending → bump quantity instead of duplicating
    existing = (
        db.query(PendingItem)
        .filter(PendingItem.barcode == barcode)
        .first()
    )
    if existing:
        existing.quantity = (existing.quantity or 1.0) + body.quantity
        db.commit()
        return {"status": "merged", "item": _row_dict(existing)}

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
    return {"status": "queued", "item": _row_dict(row)}


@router.get("/")
def list_pending(db: Session = Depends(get_db)):
    rows = db.query(PendingItem).order_by(PendingItem.created_at.desc()).all()
    return {"items": [_row_dict(r) for r in rows]}


@router.get("/count")
def pending_count(db: Session = Depends(get_db)):
    return {"count": db.query(PendingItem).count()}


@router.patch("/{item_id}")
def update_pending(item_id: int, body: PendingUpdate, db: Session = Depends(get_db)):
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
def delete_pending(item_id: int, db: Session = Depends(get_db)):
    row = db.query(PendingItem).filter(PendingItem.id == item_id).first()
    if not row:
        raise HTTPException(404, "Pending item not found")
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@router.post("/commit")
async def commit_pending(body: CommitRequest, db: Session = Depends(get_db)):
    """Import pending items into Grocy; successfully imported rows are removed."""
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
