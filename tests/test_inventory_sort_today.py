"""Items due today sort by their date, not as undated (FoodAssistant-47vjp)."""
from __future__ import annotations

from app.routers.inventory import _SORT_KEYS


def _items():
    return [{"name": "Milk", "days_remaining": 0, "amount": 1},
            {"name": "Rice", "days_remaining": 400, "amount": 1},
            {"name": "Salt", "days_remaining": None, "amount": 1},
            {"name": "Eggs", "days_remaining": 3, "amount": 1},
            {"name": "Ham", "days_remaining": -2, "amount": 1}]


def test_expiring_soonest_puts_today_before_later_dates():
    items = sorted(_items(), key=_SORT_KEYS["expiry_asc"])
    assert [i["name"] for i in items] == ["Ham", "Milk", "Eggs", "Rice", "Salt"]


def test_expiring_latest_puts_today_after_later_dates():
    items = sorted(_items(), key=_SORT_KEYS["expiry_desc"])
    assert [i["name"] for i in items] == ["Rice", "Eggs", "Milk", "Ham", "Salt"]
