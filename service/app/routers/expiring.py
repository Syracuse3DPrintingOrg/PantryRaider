from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from ..services.grocy import GrocyClient, GrocyError, error_payload
from .inventory import _ROW_STATUS, _best_by_scope_error, _is_iso_date

router = APIRouter(prefix="/expiring", tags=["expiring"])


class ExtendRequest(BaseModel):
    days: int = Field(default=3, ge=1, le=30)
    # An Expiring row's own stock: its best-by date, and its place when it
    # has one. Without them only the stock due within the page's default
    # window moves (GrocyClient.extend_best_by).
    location_id: int | None = None
    best_before_date: str | None = None


class TossRequest(BaseModel):
    # The Expiring page knows the row's amount and sends it; the Review
    # screen's toss link does not, so the amount is optional and the server
    # looks up what is actually in stock.
    amount: float | None = Field(default=None, gt=0)
    # An Expiring row is the stock of one best-by date in one place, and
    # sends both so only that stock is tossed (GrocyClient.consume_stock_group).
    location_id: int | None = None
    best_before_date: str | None = None
    # The amount the row showed when the page loaded. When the row holds
    # something else now, nothing is tossed and the answer is 409.
    expected_amount: float | None = Field(default=None, ge=0)
    # The confirmed second step after a 409 with ``place_toss``: an opened
    # unit in the same place is in the way, so toss the row's amount from that
    # place in Grocy's own order (GrocyClient.consume_stock_at_place).
    place_wide: bool = False


@router.post("/toss/{product_id}")
async def toss_item(product_id: int, body: TossRequest | None = None):
    """Toss it (FoodAssistant-64eg): the item went bad, so remove it from
    stock as spoiled rather than eaten. Grocy keeps the spoiled flag in its
    stock log, which is what the Waste summary on the Expiring page reads.
    Without an amount the product's current stock amount is tossed.

    With ``best_before_date`` and ``location_id`` only that row's stock goes,
    never the product's good stock with a later date, and the answer says how
    much of it was ``consumed`` and whether that was ``complete``. A row
    Grocy cannot toss on its own answers 409, one whose stock is gone 404.
    With ``expected_amount`` a row that holds something else than the page
    showed answers 409 and nothing is tossed.

    When an opened unit in the same place is what blocks the row, the 409
    carries ``place_toss: true``, and the page may ask once more and send
    ``place_wide``: the row's amount then goes from that place in Grocy's own
    order, opened first."""
    body = body or TossRequest()
    if body.location_id is not None and body.best_before_date is None:
        # A place alone would toss the product's stock from every place, the
        # opposite of what a caller naming one meant.
        raise HTTPException(422, "location_id needs best_before_date")
    if body.best_before_date is not None:
        problem = _best_by_scope_error(body.best_before_date, body.location_id,
                                       "best_before_date", "location_id")
        if problem:
            raise HTTPException(422, problem)
    if body.place_wide and (body.best_before_date is None or body.amount is None):
        raise HTTPException(422, "place_wide needs amount, location_id and best_before_date")
    grocy = GrocyClient()
    amount = body.amount
    try:
        if body.best_before_date is not None:
            if body.place_wide:
                result = await grocy.consume_stock_at_place(
                    product_id, amount, body.location_id, body.best_before_date,
                    spoiled=True, expected_amount=body.expected_amount)
            else:
                # Without an amount the whole row goes (best_by_take).
                result = await grocy.consume_stock_group(
                    product_id, amount if amount is not None else float("inf"),
                    body.location_id, body.best_before_date, spoiled=True,
                    expected_amount=body.expected_amount)
            _count_items_cache.invalidate()
            return {"ok": True, **result, "amount": result["consumed"]}
        if amount is None:
            for entry in await grocy.get_stock():
                if int(entry.get("product_id") or 0) == product_id:
                    amount = float(entry.get("amount") or 0)
                    break
            if not amount:
                raise HTTPException(404, "There is no stock of this item to toss.")
        await grocy.consume_stock(product_id, amount, spoiled=True)
    except GrocyError as e:
        if e.kind == "group" and getattr(e, "opened_first", False) and not body.place_wide:
            # Nothing was tossed. The page can offer the place-wide toss.
            return JSONResponse({"detail": str(e), "place_toss": True}, status_code=409)
        raise HTTPException(_ROW_STATUS.get(e.kind, 502), str(e))
    # Same reason as /extend: the count cache would otherwise show the old
    # number for up to 30s after the user just acted on this very list.
    _count_items_cache.invalidate()
    return {"ok": True, "product_id": product_id, "amount": amount}


@router.post("/extend/{product_id}")
async def extend_item(product_id: int, body: ExtendRequest):
    """Sniff test passed (FoodAssistant-fxnr): the item is still good, so push
    its best-by out by a few days (from today when the entry is already past
    its date) and return the earliest resulting date.

    An Expiring row sends its best-by date and place, and only that stock
    moves; without them, only the stock due within the page's default window
    does. Stock that is not expiring, say the same product in the freezer,
    never moves (GrocyClient.extend_best_by). A row whose stock has gone, or
    changed before any of it could be moved, answers 404.

    A date needs its place: the same date in another place, say the freezer,
    is another row and must not move with it (400)."""
    if body.location_id is not None and body.best_before_date is None:
        raise HTTPException(422, "location_id needs best_before_date")
    if body.best_before_date is not None and not _is_iso_date(body.best_before_date):
        raise HTTPException(422, "best_before_date must be a date (YYYY-MM-DD)")
    if body.best_before_date is not None and not body.location_id:
        raise HTTPException(400, "best_before_date needs location_id")
    grocy = GrocyClient()
    try:
        result = await grocy.extend_best_by(
            product_id, body.days, location_id=body.location_id,
            best_before_date=body.best_before_date)
    except GrocyError as e:
        raise HTTPException(_ROW_STATUS.get(e.kind, 502), str(e))
    # The count cache would otherwise show the old number for up to 30s after
    # the user just acted on this very list.
    _count_items_cache.invalidate()
    return {"ok": True, **result}


@router.get("/")
async def get_expiring(days: int = Query(default=7, ge=0, le=365)):
    """Items expiring within N days, sorted soonest first."""
    grocy = GrocyClient()
    try:
        return await grocy.get_expiring(days)
    except GrocyError as e:
        return JSONResponse(error_payload(e), status_code=502)


@router.get("/display", response_class=PlainTextResponse)
async def get_expiring_display(days: int = Query(default=3, ge=0, le=30)):
    """
    Plain text format for ESPHome/TFT displays.
    Each line: '<days>d: <name> x<amount>', one per product, soonest first.
    Expired items show as 'EXP'.
    """
    grocy = GrocyClient()
    try:
        rows = await grocy.get_expiring_by_date(days)
    except GrocyError:
        # A TFT display can only render plain text, so keep it short.
        return "Inventory unavailable"
    items = display_lines(rows)
    if not items:
        return "No items expiring soon"
    lines = []
    for item in items[:8]:  # TFT display limit
        d = item["days_remaining"]
        name = item["name"][:20]
        label = f"{d}d" if d >= 0 else "EXP"
        lines.append(f"{label}: {name} x{item['amount']:g}")
    return "\n".join(lines)


def display_lines(rows: list[dict]) -> list[dict]:
    """One line per product from the Expiring page's rows, soonest first. Pure.

    The amount is what the line's label means, the way the page shows it: the
    expired amount for an expired product, else the amount due on its soonest
    date, never the good stock with a later date the product total counts.
    """
    out: dict = {}
    for row in rows or []:
        pid = row.get("product_id")
        d = row["days_remaining"]
        prev = out.get(pid)
        if prev is None or d < prev["days_remaining"]:
            out[pid] = {"name": (row.get("product") or {}).get("name") or "Unknown",
                        "days_remaining": d}
    for line in out.values():
        line["amount"] = 0.0
    for row in rows or []:
        line = out[row.get("product_id")]
        d, soonest = row["days_remaining"], line["days_remaining"]
        if (d < 0 if soonest < 0 else d == soonest):
            line["amount"] += float(row.get("amount") or 0)
    return sorted(out.values(), key=lambda line: (line["days_remaining"], line["name"].lower()))


def summary_counts(items: list[dict]) -> dict:
    """Expiry bucket counts by product, each on its soonest date. Pure.

    The Expiring page lists a row per best-by date and place; counting those
    rows would put a product with two expired dates in "Expired" twice while
    /expiring/count, /summary and the Home Assistant and Bandit Cub numbers
    count it once. This counts it once, by its soonest row.
    """
    soonest: dict = {}
    for item in items or []:
        pid = item.get("product_id")
        d = item["days_remaining"]
        if pid not in soonest or d < soonest[pid]:
            soonest[pid] = d
    days = list(soonest.values())
    return {
        "expired": sum(1 for d in days if d < 0),
        "today": sum(1 for d in days if d == 0),
        "within_3_days": sum(1 for d in days if 0 < d <= 3),
        "later": sum(1 for d in days if d > 3),
    }


# Every kiosk polls /count every minute, and each uncached call was a full
# 30-day Grocy stock pull on a Pi (FoodAssistant-7dt9). Expiry counts move on
# the order of hours, so 30 seconds of staleness is invisible while collapsing
# the poll bursts from every surface into one upstream pull.
from ..services.ttl_cache import TTLCache
_count_items_cache = TTLCache(30.0)


@router.get("/count")
async def get_expiring_count(days: int = Query(default=7, ge=0, le=365)):
    """Tiny glanceable count for status faces (the on-screen Start Page key and
    the Stream Deck expiring key). Mirrors the deck's number via a shared pure
    helper, and degrades to a calm zero on any Grocy outage so a face never
    shows a stale or crashing value. Grocy pull cached ~30s (see above)."""
    from ..services.start_page import expiring_soon_count
    all_items = _count_items_cache.get()
    if all_items is None:
        grocy = GrocyClient()
        try:
            # Pull the 30-day window once, then bucket it the same way /summary
            # does so the shared count helper sees the fields the deck relies on.
            all_items = await grocy.get_expiring(days=30)
        except GrocyError:
            return {"ok": False, "count": 0}
        _count_items_cache.set(all_items)
    summary = {
        "expired": sum(1 for i in all_items if i["days_remaining"] < 0),
        "today": sum(1 for i in all_items if i["days_remaining"] == 0),
        "within_3_days": sum(1 for i in all_items if 0 < i["days_remaining"] <= 3),
        "within_7_days": sum(1 for i in all_items if 3 < i["days_remaining"] <= 7),
    }
    return {"ok": True, "count": expiring_soon_count(summary, days)}


@router.get("/summary")
async def get_expiring_summary():
    """Counts by urgency bucket: for HA sensors."""
    grocy = GrocyClient()
    try:
        all_items = await grocy.get_expiring(days=30)
    except GrocyError as e:
        return JSONResponse(error_payload(e), status_code=502)
    return {
        "expired": sum(1 for i in all_items if i["days_remaining"] < 0),
        "today": sum(1 for i in all_items if i["days_remaining"] == 0),
        "within_3_days": sum(1 for i in all_items if 0 < i["days_remaining"] <= 3),
        "within_7_days": sum(1 for i in all_items if 3 < i["days_remaining"] <= 7),
        "within_30_days": sum(1 for i in all_items if 7 < i["days_remaining"] <= 30),
    }
