from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from ..services.grocy import GrocyClient, GrocyError

router = APIRouter(prefix="/expiring", tags=["expiring"])


class ExtendRequest(BaseModel):
    days: int = Field(default=3, ge=1, le=30)


class TossRequest(BaseModel):
    # The Expiring page knows the row's amount and sends it; the Review
    # screen's toss link does not, so the amount is optional and the server
    # looks up what is actually in stock.
    amount: float | None = Field(default=None, gt=0)


@router.post("/toss/{product_id}")
async def toss_item(product_id: int, body: TossRequest | None = None):
    """Toss it (FoodAssistant-64eg): the item went bad, so remove it from
    stock as spoiled rather than eaten. Grocy keeps the spoiled flag in its
    stock log, which is what the Waste summary on the Expiring page reads.
    Without an amount the product's current stock amount is tossed."""
    grocy = GrocyClient()
    amount = body.amount if body else None
    try:
        if amount is None:
            for entry in await grocy.get_stock():
                if int(entry.get("product_id") or 0) == product_id:
                    amount = float(entry.get("amount") or 0)
                    break
            if not amount:
                raise HTTPException(404, "There is no stock of this item to toss.")
        await grocy.consume_stock(product_id, amount, spoiled=True)
    except GrocyError as e:
        raise HTTPException(502, str(e))
    # Same reason as /extend: the count cache would otherwise show the old
    # number for up to 30s after the user just acted on this very list.
    _count_items_cache.invalidate()
    return {"ok": True, "product_id": product_id, "amount": amount}


@router.post("/extend/{product_id}")
async def extend_item(product_id: int, body: ExtendRequest):
    """Sniff test passed (FoodAssistant-fxnr): the item is still good, so push
    its best-by out by a few days. Extends every dated stock entry of the
    product (from today when the entry is already past its date) and returns
    the earliest resulting date."""
    grocy = GrocyClient()
    try:
        result = await grocy.extend_best_by(product_id, body.days)
    except GrocyError as e:
        raise HTTPException(502, str(e))
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
        raise HTTPException(502, str(e))


@router.get("/display", response_class=PlainTextResponse)
async def get_expiring_display(days: int = Query(default=3, ge=0, le=30)):
    """
    Plain text format for ESPHome/TFT displays.
    Each line: '<days>d: <name> (<amount>)'
    Expired items show as '0d' or negative.
    """
    grocy = GrocyClient()
    try:
        items = await grocy.get_expiring(days)
    except GrocyError:
        # A TFT display can only render plain text, so keep it short.
        return "Inventory unavailable"
    if not items:
        return "No items expiring soon"
    lines = []
    for item in items[:8]:  # TFT display limit
        d = item["days_remaining"]
        name = item.get("product", {}).get("name", "Unknown")[:20]
        amt = item.get("amount", 1)
        label = f"{d}d" if d >= 0 else "EXP"
        lines.append(f"{label}: {name} x{int(amt)}")
    return "\n".join(lines)


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
        raise HTTPException(502, str(e))
    return {
        "expired": sum(1 for i in all_items if i["days_remaining"] < 0),
        "today": sum(1 for i in all_items if i["days_remaining"] == 0),
        "within_3_days": sum(1 for i in all_items if 0 < i["days_remaining"] <= 3),
        "within_7_days": sum(1 for i in all_items if 3 < i["days_remaining"] <= 7),
        "within_30_days": sum(1 for i in all_items if 7 < i["days_remaining"] <= 30),
    }
