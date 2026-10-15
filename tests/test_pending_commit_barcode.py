"""A committed scan links its barcode in Grocy (FoodAssistant-1rrbc)."""
from __future__ import annotations

import asyncio

import pytest

from app.database import Base, SessionLocal, engine
from app.models.db_models import PendingItem
from app.routers import pending
from app.services.grocy import GrocyClient


def _commit_one(monkeypatch, barcode):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    db.query(PendingItem).delete()
    db.add(PendingItem(barcode=barcode, name="Nutella", quantity=1.0,
                       unit="item", category="Other", storage_type="dry"))
    db.commit()
    seen = []

    async def fake_import(self, item):
        seen.append(item)
        return {"product_id": 1, "name": item.name}

    monkeypatch.setattr(GrocyClient, "import_item", fake_import)
    monkeypatch.setattr(pending.settings, "barcode_autocheck_shopping", False,
                        raising=False)
    try:
        asyncio.run(pending.commit_pending(pending.CommitRequest(), request=None, db=db))
    finally:
        db.query(PendingItem).delete()
        db.commit()
        db.close()
    assert len(seen) == 1
    return seen[0]


def test_committing_a_scanned_row_links_its_barcode(monkeypatch):
    assert _commit_one(monkeypatch, "3017620422003").barcode == "3017620422003"


@pytest.mark.parametrize("code", ["212345678901", "2001234567893", "grcy:p:12"])
def test_store_local_and_grocy_label_codes_are_not_linked(monkeypatch, code):
    assert _commit_one(monkeypatch, code).barcode is None


def test_a_row_without_a_barcode_commits_without_one(monkeypatch):
    assert _commit_one(monkeypatch, None).barcode is None
