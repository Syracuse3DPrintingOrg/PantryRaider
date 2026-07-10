from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..database import get_db
from ..models.food import ImportRequest, FoodItem, FoodItemOverride
from ..services import best_by_provenance
from ..services.defaults import apply_defaults
from ..services.grocy import GrocyClient, GrocyError
from ..storage_categories import category_keys

router = APIRouter(prefix="/inventory", tags=["inventory"])


def _apply_override(item: FoodItem, override: FoodItemOverride) -> FoodItem:
    data = item.model_dump()
    for field, value in override.model_dump(exclude_none=True).items():
        data[field] = value
    if override.best_by_date is not None:
        # A date the user set on the review screen before import is manual,
        # even if the item arrived with a "default"/"llm" source already
        # stamped on it (FoodAssistant-cidz): the override replaces the
        # guess, so the provenance must not still claim it.
        data["best_by_source"] = "manual"
    return FoodItem(**data)


@router.post("/import")
async def import_items(body: ImportRequest, db: Session = Depends(get_db)):
    """Import a list of food items into Grocy, applying overrides and defaults."""
    grocy = GrocyClient()
    results = []
    for i, item in enumerate(body.items):
        if body.overrides and i in body.overrides:
            item = _apply_override(item, body.overrides[i])
        item = apply_defaults(item, db)
        try:
            result = await grocy.import_item(item)
            results.append({"index": i, "status": "ok", **result})
            # Record how the best-by date was worked out, now that the item has
            # a Grocy product id (FoodAssistant-cidz). Recorded only when a date
            # actually exists; best_by_provenance quietly no-ops for "manual"
            # (or unset), the no-badge default anyway.
            if item.best_by_date is not None:
                best_by_provenance.record(
                    result.get("product_id"), item.name,
                    item.best_by_source or "manual",
                    item.best_by_date.isoformat(),
                )
        except Exception as e:
            results.append({"index": i, "status": "error", "error": str(e)})
    return {"imported": len([r for r in results if r["status"] == "ok"]), "results": results}


@router.post("/consume/{product_id}")
async def consume_item(product_id: int, amount: float = 1.0):
    """Mark stock as consumed in Grocy."""
    grocy = GrocyClient()
    try:
        return await grocy.consume_stock(product_id, amount)
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/stock")
async def get_stock():
    """Return full stock list from Grocy."""
    grocy = GrocyClient()
    try:
        return await grocy.get_stock()
    except GrocyError as e:
        raise HTTPException(502, str(e))


_SORT_KEYS = {
    "expiry_asc":  lambda i: (i["days_remaining"] is None,  i["days_remaining"] or 9999, i["name"].lower()),
    "expiry_desc": lambda i: (i["days_remaining"] is None, -(i["days_remaining"] or -9999), i["name"].lower()),
    "name_asc":    lambda i: i["name"].lower(),
    "name_desc":   lambda i: i["name"].lower(),
    "qty_desc":    lambda i: -i["amount"],
    "qty_asc":     lambda i:  i["amount"],
    # ISO timestamps sort lexicographically; items without one sort last.
    # added_desc is applied with reverse=True, so its tuple is inverted
    # ("is not None" first) to keep undated items at the bottom either way.
    "added_asc":   lambda i: (i.get("added_date") is None, i.get("added_date") or "", i["name"].lower()),
    "added_desc":  lambda i: (i.get("added_date") is not None, i.get("added_date") or "", i["name"].lower()),
}
_REVERSED_SORTS = {"name_desc", "added_desc"}


class MoveRequest(BaseModel):
    bucket: str  # any built-in or custom category key (grocy.move_product validates)


class EditRequest(BaseModel):
    category: str | None = None
    best_before_date: str | None = None  # YYYY-MM-DD or empty string to clear


@router.patch("/edit/{product_id}")
async def edit_item(product_id: int, body: EditRequest):
    """Update category and/or best-by date for a product's stock entries."""
    grocy = GrocyClient()
    try:
        bbd = body.best_before_date if body.best_before_date else None
        return await grocy.edit_product(product_id, body.category, bbd)
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/move/{product_id}")
async def move_item(product_id: int, body: MoveRequest):
    """Move all stock of a product to a different storage location."""
    grocy = GrocyClient()
    try:
        return await grocy.move_product(product_id, body.bucket)
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/dashboard")
async def get_dashboard(sort: str = "expiry_asc"):
    """Return stock grouped by storage bucket, sorted by the requested key."""
    grocy = GrocyClient()
    try:
        items = await grocy.get_full_stock()
    except GrocyError as e:
        # 502 with honest copy, never a raw 500: the dashboard renders the
        # detail as its outage banner (FoodAssistant-2cmm).
        raise HTTPException(502, str(e))

    key_fn = _SORT_KEYS.get(sort, _SORT_KEYS["expiry_asc"])
    items.sort(key=key_fn, reverse=sort in _REVERSED_SORTS)

    # Built-in + custom buckets, plus "other" for anything unclassified.
    buckets = category_keys() + ["other"]
    return {
        bucket: [i for i in items if i["storage_bucket"] == bucket]
        for bucket in buckets
    }
