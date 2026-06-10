from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from ..database import get_db
from ..models.food import ImportRequest, FoodItem, FoodItemOverride
from ..services.defaults import apply_defaults
from ..services.grocy import GrocyClient

router = APIRouter(prefix="/inventory", tags=["inventory"])


def _apply_override(item: FoodItem, override: FoodItemOverride) -> FoodItem:
    data = item.model_dump()
    for field, value in override.model_dump(exclude_none=True).items():
        data[field] = value
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
    return await grocy.get_stock()


_SORT_KEYS = {
    "expiry_asc":  lambda i: (i["days_remaining"] is None,  i["days_remaining"] or 9999, i["name"].lower()),
    "expiry_desc": lambda i: (i["days_remaining"] is None, -(i["days_remaining"] or -9999), i["name"].lower()),
    "name_asc":    lambda i: i["name"].lower(),
    "name_desc":   lambda i: i["name"].lower(),
    "qty_desc":    lambda i: -i["amount"],
    "qty_asc":     lambda i:  i["amount"],
}

_BUCKETS = ["refrigerated", "frozen", "room_temp", "pantry", "other"]


@router.get("/dashboard")
async def get_dashboard(sort: str = "expiry_asc"):
    """Return stock grouped by storage bucket, sorted by the requested key."""
    grocy = GrocyClient()
    items = await grocy.get_full_stock()

    key_fn = _SORT_KEYS.get(sort, _SORT_KEYS["expiry_asc"])
    reverse = sort == "name_desc"
    items.sort(key=key_fn, reverse=reverse)

    return {
        bucket: [i for i in items if i["storage_bucket"] == bucket]
        for bucket in _BUCKETS
    }
