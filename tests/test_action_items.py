"""Action Items (notifications) store, generator, and inbox API
(FoodAssistant-iut3, -7zzv)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.database import SessionLocal  # noqa: E402
from app.models.db_models import ActionItem  # noqa: E402
from app.services import action_items as ai  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_table():
    db = SessionLocal()
    db.query(ActionItem).delete()
    db.commit()
    db.close()
    yield
    db = SessionLocal()
    db.query(ActionItem).delete()
    db.commit()
    db.close()


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


# -- store ------------------------------------------------------------------

def test_create_and_list_active(db):
    ai.create(db, ai.KIND_GENERIC, "Hello", body="world", level="info")
    items = ai.list_active(db)
    assert len(items) == 1
    assert items[0]["title"] == "Hello" and items[0]["body"] == "world"
    assert items[0]["status"] == "open"


def test_create_dedupes_on_key(db):
    a = ai.create(db, ai.KIND_FOOD_EXPIRED, "Milk expired", dedupe_key="k1", level="error")
    b = ai.create(db, ai.KIND_FOOD_EXPIRED, "Milk has expired", dedupe_key="k1", level="error")
    assert a["id"] == b["id"]                 # same row reused
    assert ai.count_active(db) == 1
    assert ai.get(db, a["id"])["title"] == "Milk has expired"  # content refreshed


def test_snooze_hides_until_due_then_returns(db, monkeypatch):
    item = ai.create(db, ai.KIND_GENERIC, "Snooze me")
    ai.snooze(db, item["id"], hours=24)
    assert ai.count_active(db) == 0           # hidden while snoozed
    # Force the snooze into the past: it should reappear as active.
    row = db.get(ActionItem, item["id"])
    row.snooze_until = "2000-01-01T00:00:00+00:00"
    db.commit()
    assert ai.count_active(db) == 1


def test_archive_and_resolve_remove_from_inbox(db):
    a = ai.create(db, ai.KIND_GENERIC, "A")
    b = ai.create(db, ai.KIND_GENERIC, "B")
    ai.archive(db, a["id"])
    ai.resolve(db, b["id"])
    assert ai.count_active(db) == 0


def test_dedupe_refreshes_without_reviving(db):
    # An archived item for a still-expired product STAYS archived when the
    # generator re-fires (FoodAssistant-wf62: reviving here undid every archive
    # on the next inbox poll, because an expired product stays expired).
    a = ai.create(db, ai.KIND_FOOD_EXPIRED, "Eggs expired", dedupe_key="k2")
    ai.archive(db, a["id"])
    assert ai.count_active(db) == 0
    again = ai.create(db, ai.KIND_FOOD_EXPIRED, "Eggs expired (2d)", dedupe_key="k2")
    assert again["id"] == a["id"]           # deduped onto the same row
    assert again["title"] == "Eggs expired (2d)"  # content refreshed
    assert ai.count_active(db) == 0         # but the archive sticks


def test_snooze_sticks_across_generator_sweeps(db):
    # A snoozed item must stay hidden through generator re-fires and only come
    # back when the snooze elapses (FoodAssistant-wf62).
    items = [{"product_id": 9, "product": {"name": "Ham"}, "days_remaining": -1,
              "best_before_date": "2026-06-30"}]
    ai.sync_food_expired(db, items)
    item = ai.list_active(db)[0]
    ai.snooze(db, item["id"], hours=24)
    assert ai.count_active(db) == 0
    ai.sync_food_expired(db, items)         # product still expired
    assert ai.count_active(db) == 0         # snooze holds
    row = db.query(ActionItem).get(item["id"])
    row.snooze_until = "2000-01-01T00:00:00+00:00"
    db.commit()
    ai.sync_food_expired(db, items)
    assert ai.count_active(db) == 1         # due again: alert returns


def test_new_expiry_after_recovery_raises_a_fresh_item(db):
    # Product expires, user archives, product gets consumed (leaves the expired
    # set, key retired), then a NEW batch expires: a fresh alert must appear
    # instead of being swallowed by the old archived row.
    items = [{"product_id": 5, "product": {"name": "Milk"}, "days_remaining": -1,
              "best_before_date": "2026-06-30"}]
    ai.sync_food_expired(db, items)
    first = ai.list_active(db)[0]
    ai.archive(db, first["id"])
    ai.sync_food_expired(db, [])            # consumed: key retired
    ai.sync_food_expired(db, items)         # new batch expired
    active = ai.list_active(db)
    assert len(active) == 1
    assert active[0]["id"] != first["id"]


# -- food-expired generator -------------------------------------------------

def test_sync_food_expired_raises_and_archives_stale(db):
    items = [
        {"product_id": 1, "product": {"name": "Milk"}, "days_remaining": -3, "best_before_date": "2026-06-01", "amount": 1},
        {"product_id": 2, "product": {"name": "Yogurt"}, "days_remaining": 0, "best_before_date": "2026-06-29", "amount": 2},
        {"product_id": 3, "product": {"name": "Apples"}, "days_remaining": 4},  # not expired: skipped
    ]
    n = ai.sync_food_expired(db, items)
    assert n == 2
    active = ai.list_active(db)
    titles = {i["title"] for i in active}
    assert "Milk has expired" in titles
    assert "Yogurt expires today" in titles
    assert all("Apples" not in t for t in titles)
    # Next sweep finds nothing expired: the prior items are auto-archived.
    assert ai.sync_food_expired(db, []) == 0
    assert ai.count_active(db) == 0


# -- inbox API --------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    # Skip the throttled Grocy refresh so the list endpoint stays offline-safe.
    import app.routers.action_items as r
    monkeypatch.setattr(r, "_last_refresh", 1e18, raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_archive_all_clears_the_inbox(db):
    ai.create(db, ai.KIND_GENERIC, "A")
    ai.create(db, ai.KIND_GENERIC, "B")
    ai.create(db, ai.KIND_FOOD_EXPIRED, "Milk", dedupe_key="k")
    assert ai.count_active(db) == 3
    n = ai.archive_all(db)
    assert n == 3
    assert ai.count_active(db) == 0


def test_archive_all_endpoint(client):
    db = SessionLocal()
    ai.create(db, ai.KIND_GENERIC, "One")
    ai.create(db, ai.KIND_GENERIC, "Two")
    db.close()
    r = client.post("/action-items/archive-all").json()
    assert r["ok"] and r["archived"] == 2
    assert client.get("/action-items").json()["count"] == 0


def test_inbox_endpoints_list_and_act(client):
    db = SessionLocal()
    item = ai.create(db, ai.KIND_GENERIC, "Do a thing")
    db.close()
    listed = client.get("/action-items").json()
    assert listed["count"] == 1 and listed["items"][0]["title"] == "Do a thing"
    # Snooze removes it from the active list.
    assert client.post(f"/action-items/{item['id']}/snooze", json={"hours": 24}).json()["ok"]
    assert client.get("/action-items").json()["count"] == 0
    # Archive on an unknown id is a clean no-op, not a crash.
    assert client.post("/action-items/999999/archive").json()["ok"] is False
