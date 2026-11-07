import math
import re
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..config import settings
from ..database import get_db
from ..models.food import ImportRequest, FoodItem, FoodItemOverride
from ..services import best_by_provenance
from ..services import defaults as defaults_service
from ..services.defaults import apply_defaults
from ..services.grocy import GrocyClient, GrocyError, error_payload
from ..storage_categories import category_keys

router = APIRouter(prefix="/inventory", tags=["inventory"])

# A per-date row's action that Grocy cannot do on exactly that row's stock
# answers 409, and one whose stock is gone answers 404, both with nothing
# changed. A row that no longer holds what the page showed answers 409 too.
# They are caught before the catch-all, which would make them 500s.
_ROW_STATUS = {"group": 409, "gone": 404, "changed": 409}

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _is_iso_date(value: str | None) -> bool:
    """True for a real YYYY-MM-DD date. Grocy stores anything else as no
    date at all. Pure."""
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _best_by_scope_error(best_before_date: str | None, location_id: int | None,
                         date_field: str, location_field: str) -> str | None:
    """What is wrong with a per-date row's scope, or None. Pure.

    The date is the row's token, an ISO date or "none" for stock with no
    best-by date, and it only means something together with the row's place.
    """
    if best_before_date != "none" and not _is_iso_date(best_before_date):
        return f"{date_field} must be a date (YYYY-MM-DD) or none"
    if not location_id:
        return f"{date_field} needs {location_field}"
    return None


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
        item = await run_in_threadpool(apply_defaults, item, db)
        try:
            result = await grocy.import_item(item)
            results.append({"index": i, "status": "ok", **result})
            # Record how the best-by date was worked out, now that the item has
            # a Grocy product id (FoodAssistant-cidz). Recorded only when a date
            # actually exists; best_by_provenance quietly no-ops for "manual"
            # (or unset), the no-badge default anyway.
            if item.best_by_date is not None:
                await run_in_threadpool(
                    best_by_provenance.record,
                    result.get("product_id"), item.name,
                    item.best_by_source or "manual",
                    item.best_by_date.isoformat(),
                )
        except Exception as e:
            results.append({"index": i, "status": "error", "error": str(e)})
    return {"imported": len([r for r in results if r["status"] == "ok"]), "results": results}


@router.post("/consume/{product_id}")
async def consume_item(product_id: int, amount: float = 1.0,
                       location_id: int | None = None,
                       best_before_date: str | None = None):
    """Mark stock as consumed in Grocy, optionally from one location only.

    ``best_before_date`` (with ``location_id``) takes only a per-date row's
    own stock, and answers how much of it was used
    (GrocyClient.consume_stock_group)."""
    if best_before_date is not None:
        problem = _best_by_scope_error(best_before_date, location_id,
                                       "best_before_date", "location_id")
        if problem is None and not amount > 0:
            problem = "amount must be more than 0"
        if problem:
            raise HTTPException(422, problem)
    grocy = GrocyClient()
    try:
        if best_before_date is not None:
            return await grocy.consume_stock_group(product_id, amount, location_id,
                                                   best_before_date)
        return await grocy.consume_stock(product_id, amount,
                                         location_id=location_id)
    except GrocyError as e:
        raise HTTPException(_ROW_STATUS.get(e.kind, 500), str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/open/{product_id}")
async def open_item(product_id: int, amount: float = 1.0,
                    location_id: int | None = None,
                    best_before_date: str | None = None):
    """Mark one unit of a product opened in Grocy (FoodAssistant-oyef).

    Grocy then applies the product's after-opening shelf-life rule, so an
    opened jar's best-by date turns honest on its own.

    ``location_id`` with ``best_before_date`` (both or neither) opens one
    unit of a per-date row's own stock (GrocyClient.open_stock_group)."""
    scoped = location_id is not None or best_before_date is not None
    if scoped:
        problem = ("best_before_date and location_id go together"
                   if best_before_date is None
                   else _best_by_scope_error(best_before_date, location_id,
                                             "best_before_date", "location_id"))
        if problem:
            raise HTTPException(422, problem)
    grocy = GrocyClient()
    try:
        if scoped:
            return await grocy.open_stock_group(product_id, location_id, best_before_date)
        return await grocy.open_stock(product_id, amount)
    except GrocyError as e:
        raise HTTPException(_ROW_STATUS.get(e.kind, 500), str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/stock")
async def get_stock():
    """Return full stock list from Grocy."""
    grocy = GrocyClient()
    try:
        return await grocy.get_stock()
    except GrocyError as e:
        return JSONResponse(error_payload(e), status_code=502)


_SORT_KEYS = {
    # Undated items sort last either way. Test for None, not falsiness: an item
    # due today has 0 days left and belongs at the top of "soonest".
    "expiry_asc":  lambda i: (i["days_remaining"] is None,
                              0 if i["days_remaining"] is None else i["days_remaining"],
                              i["name"].lower()),
    "expiry_desc": lambda i: (i["days_remaining"] is None,
                              0 if i["days_remaining"] is None else -i["days_remaining"],
                              i["name"].lower()),
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

# The dashboard key that carries the Grocy product group list.
PRODUCT_GROUPS_KEY = "_product_groups"


class MoveRequest(BaseModel):
    bucket: str  # any built-in or custom category key (grocy.move_product validates)
    # Move only the stock at this Grocy location; omitted moves all of it.
    from_location_id: int | None = None
    # With from_location_id, move only the stock there with this best-by date
    # (YYYY-MM-DD, or "none" for no date): one per-date row.
    from_best_before_date: str | None = None
    # With from_location_id, move only this much of the stock in scope; at
    # least what is there (or omitted) moves all of it.
    amount: float | None = None


class EditRequest(BaseModel):
    category: str | None = None
    best_before_date: str | None = None  # YYYY-MM-DD or empty string to clear
    # A per-date row's own place and best-by date (YYYY-MM-DD, or "none"),
    # both or neither: the new date then lands on that row's stock only.
    from_location_id: int | None = None
    from_best_before_date: str | None = None


@router.patch("/edit/{product_id}")
async def edit_item(product_id: int, body: EditRequest):
    """Update category and/or best-by date for a product's stock entries."""
    bbd = body.best_before_date if body.best_before_date else None
    scoped = body.from_location_id is not None or body.from_best_before_date is not None
    if scoped:
        problem = ("from_best_before_date and from_location_id go together"
                   if body.from_best_before_date is None
                   else _best_by_scope_error(body.from_best_before_date,
                                             body.from_location_id,
                                             "from_best_before_date", "from_location_id"))
        if problem is None and bbd is not None and not _is_iso_date(bbd):
            # Grocy would store it as no date at all, and the next merge
            # would make that stick.
            problem = "best_before_date must be a date (YYYY-MM-DD)"
        if problem:
            raise HTTPException(422, problem)
    grocy = GrocyClient()
    try:
        if scoped:
            return await grocy.edit_product(
                product_id, body.category, bbd,
                from_location_id=body.from_location_id,
                from_best_before_date=body.from_best_before_date)
        return await grocy.edit_product(product_id, body.category, bbd)
    except GrocyError as e:
        raise HTTPException(_ROW_STATUS.get(e.kind, 500), str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/move/{product_id}")
async def move_item(product_id: int, body: MoveRequest, db: Session = Depends(get_db)):
    """Move all stock of a product to a different storage location, or only
    the stock at ``from_location_id`` when the body names one.

    When the move crosses temperature natures (fridge to freezer, freezer to
    fridge, pantry to fridge, ...), the best-by date follows
    (FoodAssistant-jty6): the destination shelf-life rule is looked up once
    here (the user's own rule, a community override, or the built-in default)
    and the pure proposal in defaults.propose_transfer_best_by decides each
    entry's new date. Moves into a custom bucket or "other" never touch dates.

    ``from_best_before_date`` (which needs ``from_location_id``) moves one
    per-date row's own stock, and the answer adds how much was
    ``left_behind``. ``amount`` (which needs ``from_location_id`` too) moves
    only that much of the stock in scope; ``left_behind`` then counts only
    what was asked for and did not move.
    """
    if body.from_best_before_date is not None:
        problem = _best_by_scope_error(body.from_best_before_date, body.from_location_id,
                                       "from_best_before_date", "from_location_id")
        if problem:
            raise HTTPException(422, problem)
    if body.amount is not None:
        if not math.isfinite(body.amount) or body.amount <= 0:
            raise HTTPException(422, "amount must be more than 0")
        if body.from_location_id is None:
            raise HTTPException(422, "amount needs from_location_id")
    grocy = GrocyClient()
    try:
        proposer = None
        to_kind = defaults_service.storage_kind_for_bucket(body.bucket)
        if to_kind:
            name, group = await grocy.product_name_and_group(product_id)
            dest_days = await run_in_threadpool(
                defaults_service.resolve_rule_days, db, name, group, to_kind)

            def proposer(old_best_by, from_bucket, _days=dest_days, _to=to_kind):
                return defaults_service.propose_transfer_best_by(
                    old_best_by,
                    defaults_service.storage_kind_for_bucket(from_bucket),
                    _to, _days)

        # Only the scope the body names is passed on, so a caller that never
        # sends an amount reaches move_product exactly as before.
        scope = {}
        if body.from_best_before_date is not None:
            scope["from_best_before_date"] = body.from_best_before_date
        if body.amount is not None:
            scope["amount"] = body.amount
        return await grocy.move_product(product_id, body.bucket,
                                        propose_best_by=proposer,
                                        from_location_id=body.from_location_id,
                                        **scope)
    except GrocyError as e:
        raise HTTPException(_ROW_STATUS.get(e.kind, 500), str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/dashboard")
async def get_dashboard(sort: str = "expiry_asc", dates: str = ""):
    """Return stock grouped by storage bucket, sorted by the requested key.

    ``dates=breakdown`` is the Inventory page's own reading: a row whose
    stock has more than one best-by date also lists them. ``dates=split``
    gives a place holding more than one date a row per date instead, and
    ``dates=auto`` is whichever of the two the "One row per best-by date"
    setting picks. Without it the reply is the one the Home Assistant sensor
    has always read.
    """
    if dates == "auto":
        dates = "split" if settings.inventory_rows_per_best_by else "breakdown"
    grocy = GrocyClient()
    try:
        # Any other value reads the rows through the plain call, the one every
        # other caller of get_full_stock makes.
        if dates in ("breakdown", "split"):
            items = await grocy.get_full_stock(dates=dates)
        else:
            items = await grocy.get_full_stock()
    except GrocyError as e:
        # 502 with honest copy, never a raw 500: the dashboard renders the
        # detail as its outage banner (FoodAssistant-2cmm). A Grocy-reported
        # setup problem also carries the fix hint and whether this device can
        # apply it, so the banner never promises it "comes back on its own".
        return JSONResponse(error_payload(e), status_code=502)

    # amount_opened for the Opened badge is merged inside get_full_stock, out
    # of the same /stock payload it already reads (FoodAssistant-oyef, ydj2a).
    key_fn = _SORT_KEYS.get(sort, _SORT_KEYS["expiry_asc"])
    items.sort(key=key_fn, reverse=sort in _REVERSED_SORTS)

    # Built-in + custom buckets, plus "other" for anything unclassified.
    buckets = category_keys() + ["other"]
    out = {
        bucket: [i for i in items if i["storage_bucket"] == bucket]
        for bucket in buckets
    }
    # Every Grocy product group, custom ones included, for the page's category
    # filter (FoodAssistant-7xddm). The underscore keeps it apart from the
    # bucket keys, and a custom bucket that somehow took the same key wins.
    if PRODUCT_GROUPS_KEY not in out:
        out[PRODUCT_GROUPS_KEY] = grocy.dashboard_product_groups()
    return out
