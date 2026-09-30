"""The monthly AI token budget covers every AI call made on the way in, and the
per-item shelf-life estimates are bounded.

Settings promises that AI features pause once the budget is reached, but only
photo, receipt, and receipt-price analysis checked it: barcode enrichment, the
barcode identify fallback, and the shelf-life estimate for each scanned item
kept calling the provider. A receipt also awaited one shelf-life call per
line, one after another, with no cap on how many.
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

from app import dependencies  # noqa: E402
from app.config import settings  # noqa: E402
from app.models.food import AnalysisResult, FoodItem  # noqa: E402
from app.providers.base import VisionProvider  # noqa: E402
from app.routers import analyze  # noqa: E402
from app.services import barcode, usage  # noqa: E402


class _CountingProvider(VisionProvider):
    """Records every text call it gets, and how many ran at once."""

    def __init__(self, reply=None, delay: float = 0.0):
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.reply = reply if reply is not None else {
            "name": "Whole Milk", "category": "Dairy",
            "storage_type": "refrigerated", "shelf_life_days": 9}
        self.delay = delay

    async def analyze_food(self, image_data, mime_type):
        raise NotImplementedError

    async def analyze_receipt(self, image_data, mime_type):
        raise NotImplementedError

    async def health_check(self):
        return True

    async def enrich_product(self, info):
        self.calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            return dict(self.reply)
        finally:
            self.in_flight -= 1

    async def identify_barcode(self, code):
        self.calls += 1
        return {"name": "Mystery Soda", "category": "Beverages",
                "storage_type": "room_temp", "shelf_life_days": 180}


@pytest.fixture
def provider(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    fake = _CountingProvider()
    monkeypatch.setattr(dependencies, "get_enrich_provider", lambda: fake)
    return fake


def _budget(monkeypatch, over: bool) -> None:
    monkeypatch.setattr(usage, "over_budget", lambda *a, **k: over)


def _off_item() -> FoodItem:
    return FoodItem(name="Zero Sugar", brand="Dr Pepper", confidence=0.9)


# --- barcode scans --------------------------------------------------------------

def test_barcode_enrichment_makes_no_call_over_budget(provider, monkeypatch):
    monkeypatch.setattr(settings, "barcode_enrichment", "llm")
    _budget(monkeypatch, True)
    item = _off_item()
    enriched = asyncio.run(barcode._llm_enrich(
        item, {"product_name": "zero sugar", "brands": "Dr Pepper"}, "", []))
    assert enriched is False  # the caller falls back to the heuristics
    assert provider.calls == 0
    assert item.name == "Zero Sugar"


def test_barcode_enrichment_still_runs_under_budget(provider, monkeypatch):
    monkeypatch.setattr(settings, "barcode_enrichment", "llm")
    _budget(monkeypatch, False)
    item = _off_item()
    assert asyncio.run(barcode._llm_enrich(item, {"product_name": "zero sugar"}, "", []))
    assert provider.calls == 1
    assert item.name == "Whole Milk"


def test_enriched_name_is_capped(provider, monkeypatch):
    monkeypatch.setattr(settings, "barcode_enrichment", "llm")
    _budget(monkeypatch, False)
    provider.reply = {"name": "Organic " * 60, "brand": "B" * 300}
    item = _off_item()
    asyncio.run(barcode._llm_enrich(item, {"product_name": "x"}, "", []))
    assert 0 < len(item.name) <= 120
    assert len(item.brand) <= 80


def test_blank_enriched_name_keeps_the_scanned_name(provider, monkeypatch):
    monkeypatch.setattr(settings, "barcode_enrichment", "llm")
    _budget(monkeypatch, False)
    provider.reply = {"name": "   ", "brand": ["not", "text"]}
    item = _off_item()
    asyncio.run(barcode._llm_enrich(item, {"product_name": "x"}, "", []))
    assert item.name == "Zero Sugar"
    assert item.brand == "Dr Pepper"


def test_identify_fallback_makes_no_call_over_budget(provider, monkeypatch):
    _budget(monkeypatch, True)
    assert asyncio.run(barcode._llm_identify_barcode("0049000000443")) is None
    assert provider.calls == 0


def test_identify_fallback_still_runs_under_budget(provider, monkeypatch):
    _budget(monkeypatch, False)
    item = asyncio.run(barcode._llm_identify_barcode("0049000000443"))
    assert provider.calls == 1
    assert item.name == "Mystery Soda (unverified guess)"


def test_infinite_shelf_life_from_the_identify_fallback_is_ignored(provider, monkeypatch):
    _budget(monkeypatch, False)

    async def identify(code):
        return {"name": "Mystery Soda", "shelf_life_days": float("inf")}

    monkeypatch.setattr(provider, "identify_barcode", identify)
    item = asyncio.run(barcode._llm_identify_barcode("0049000000443"))
    assert item.name == "Mystery Soda (unverified guess)"
    assert item.best_by_date is None


# --- shelf-life estimates -------------------------------------------------------

def test_shelf_life_estimate_makes_no_call_over_budget(provider, monkeypatch):
    _budget(monkeypatch, True)
    item = FoodItem(name="Milk")
    asyncio.run(analyze._llm_shelf_life(item))
    assert provider.calls == 0
    assert item.best_by_date is None


def test_shelf_life_estimate_still_runs_under_budget(provider, monkeypatch):
    _budget(monkeypatch, False)
    item = FoodItem(name="Milk")
    asyncio.run(analyze._llm_shelf_life(item))
    assert provider.calls == 1
    assert item.best_by_date == date.today() + timedelta(days=9)


def test_a_garbled_estimate_never_breaks_intake(provider, monkeypatch):
    # json.loads reads 1e999 as infinity; the shelf-life mapper cannot turn
    # that into a day count, and that must cost only the estimate.
    _budget(monkeypatch, False)
    provider.reply = {"shelf_life_days": float("inf"), "storage_type": "frozen"}
    items = [FoodItem(name="Peas"), FoodItem(name="Corn")]
    asyncio.run(analyze._estimate_shelf_lives(items))
    assert provider.calls == 2
    assert all(i.best_by_date is None for i in items)


def test_fan_out_is_capped_and_concurrent(provider, monkeypatch):
    _budget(monkeypatch, False)
    provider.delay = 0.01
    items = [FoodItem(name=f"Item {n}") for n in range(100)]
    asyncio.run(analyze._estimate_shelf_lives(items))
    assert provider.calls == 60
    assert provider.max_in_flight == 4
    assert all(i.best_by_date is not None for i in items[:60])
    assert all(i.best_by_date is None for i in items[60:])


def test_fan_out_stops_when_the_budget_runs_out(provider, monkeypatch):
    monkeypatch.setattr(usage, "over_budget", lambda *a, **k: provider.calls >= 10)
    provider.delay = 0.01
    items = [FoodItem(name=f"Item {n}") for n in range(40)]
    asyncio.run(analyze._estimate_shelf_lives(items))
    assert provider.calls == 10
    assert sum(1 for i in items if i.best_by_date is not None) == 10


def test_items_with_a_printed_date_are_not_estimated(provider, monkeypatch):
    _budget(monkeypatch, False)
    printed = date(2027, 1, 31)
    items = [FoodItem(name="Yogurt", best_by_date=printed), FoodItem(name="Milk")]
    asyncio.run(analyze._estimate_shelf_lives(items))
    assert provider.calls == 1
    assert items[0].best_by_date == printed


# --- the receipt route ----------------------------------------------------------

class _ReceiptProvider(VisionProvider):
    def __init__(self, count: int):
        self.count = count

    async def analyze_food(self, image_data, mime_type):
        raise NotImplementedError

    async def analyze_receipt(self, image_data, mime_type):
        return AnalysisResult(items=[FoodItem(name=f"Line {n}") for n in range(self.count)],
                              image_type="receipt")

    async def health_check(self):
        return True


@pytest.fixture
def client(monkeypatch, tmp_path, provider):
    cwd = os.getcwd()
    os.chdir(_SERVICE)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test")
    monkeypatch.setattr(settings, "grocy_api_key", "k")
    monkeypatch.setattr(settings, "vision_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    monkeypatch.setattr(settings, "llm_expiry_enabled", True)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    from app.main import app
    try:
        yield app, TestClient(app)
    finally:
        app.dependency_overrides.clear()
        os.chdir(cwd)


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 180, 160)).save(buf, format="PNG")
    return buf.getvalue()


def test_long_receipt_estimates_at_most_60_lines(client, provider, monkeypatch):
    app, c = client
    _budget(monkeypatch, False)
    app.dependency_overrides[dependencies.get_vision_provider] = lambda: _ReceiptProvider(75)
    r = c.post("analyze/receipt", files={"file": ("r.png", _png(), "image/png")})
    assert r.status_code == 200
    assert len(r.json()["items"]) == 75
    assert provider.calls == 60


# --- the settings copy ----------------------------------------------------------

def test_settings_describe_the_budget_the_way_it_behaves():
    source = (_SERVICE / "app" / "templates" / "setup" / "_pane_scanning.html").read_text()
    assert "When the budget is reached, AI features pause until next month or you raise it." in source
    assert "barcode enrichment are declined" not in source
