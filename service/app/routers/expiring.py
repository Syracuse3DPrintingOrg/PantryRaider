from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse
from ..services.grocy import GrocyClient

router = APIRouter(prefix="/expiring", tags=["expiring"])


@router.get("/")
async def get_expiring(days: int = Query(default=7, ge=0, le=365)):
    """Items expiring within N days, sorted soonest first."""
    grocy = GrocyClient()
    return await grocy.get_expiring(days)


@router.get("/display", response_class=PlainTextResponse)
async def get_expiring_display(days: int = Query(default=3, ge=0, le=30)):
    """
    Plain text format for ESPHome/TFT displays.
    Each line: '<days>d: <name> (<amount>)'
    Expired items show as '0d' or negative.
    """
    grocy = GrocyClient()
    items = await grocy.get_expiring(days)
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


@router.get("/summary")
async def get_expiring_summary():
    """Counts by urgency bucket — for HA sensors."""
    grocy = GrocyClient()
    all_items = await grocy.get_expiring(days=30)
    return {
        "expired": sum(1 for i in all_items if i["days_remaining"] < 0),
        "today": sum(1 for i in all_items if i["days_remaining"] == 0),
        "within_3_days": sum(1 for i in all_items if 0 < i["days_remaining"] <= 3),
        "within_7_days": sum(1 for i in all_items if 3 < i["days_remaining"] <= 7),
        "within_30_days": sum(1 for i in all_items if 7 < i["days_remaining"] <= 30),
    }
