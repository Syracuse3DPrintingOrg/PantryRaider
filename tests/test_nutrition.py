"""Food-intake / nutrition log: store, totals, and API (FoodAssistant-e6qt)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.database import SessionLocal  # noqa: E402
from app.models.db_models import IntakeLog  # noqa: E402
from app.services import nutrition  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    db = SessionLocal()
    db.query(IntakeLog).delete()
    db.commit()
    db.close()
    yield
    db = SessionLocal()
    db.query(IntakeLog).delete()
    db.commit()
    db.close()


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def test_day_totals_sums_and_tolerates_missing():
    entries = [
        {"calories": 200, "protein": 10, "carbs": 20, "fat": 5},
        {"calories": 150, "protein": 8, "carbs": None, "fat": 3},  # missing carbs
        {"calories": None, "protein": None, "carbs": None, "fat": None},  # all blank
    ]
    t = nutrition.day_totals(entries)
    assert t["calories"] == 350 and t["protein"] == 18
    assert t["carbs"] == 20 and t["fat"] == 8
    assert t["count"] == 3


def test_log_and_list_for_today(db):
    nutrition.log_intake(db, "Eggs", 2, calories=140, protein=12, carbs=1, fat=10)
    nutrition.log_intake(db, "Toast", 1, calories=80, protein=3, carbs=15, fat=1)
    entries = nutrition.list_for_date(db)
    assert {e["name"] for e in entries} == {"Eggs", "Toast"}
    totals = nutrition.day_totals(entries)
    assert totals["calories"] == 220 and totals["protein"] == 15


def test_delete_entry(db):
    e = nutrition.log_intake(db, "Snack", 1, calories=100)
    assert nutrition.delete(db, e["id"]) is True
    assert nutrition.list_for_date(db) == []
    assert nutrition.delete(db, 999999) is False


def test_entries_are_scoped_to_their_day(db):
    nutrition.log_intake(db, "Yesterday lunch", 1, calories=500, date="2000-01-01")
    nutrition.log_intake(db, "Today snack", 1, calories=100)
    today = nutrition.list_for_date(db)
    assert [e["name"] for e in today] == ["Today snack"]
    old = nutrition.list_for_date(db, "2000-01-01")
    assert [e["name"] for e in old] == ["Yesterday lunch"]


# -- API --------------------------------------------------------------------

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
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_log_today_delete_endpoints(client):
    r = client.post("/nutrition/log", json={"name": "Banana", "servings": 1,
                                            "calories": 105, "protein": 1.3, "carbs": 27, "fat": 0.4})
    assert r.status_code == 200
    eid = r.json()["entry"]["id"]
    day = client.get("/nutrition/today").json()
    assert day["totals"]["calories"] == 105 and len(day["entries"]) == 1
    assert client.delete(f"/nutrition/{eid}").json()["ok"] is True
    assert client.get("/nutrition/today").json()["totals"]["calories"] == 0


def test_estimate_endpoint_uses_provider(client, monkeypatch):
    import app.routers.nutrition as nrouter

    class FakeProvider:
        async def estimate_nutrition(self, name, servings=1.0):
            return {"calories": 95 * servings, "protein": 0.5, "carbs": 25, "fat": 0.3}

    monkeypatch.setattr(nrouter, "get_enrich_provider", lambda: FakeProvider(), raising=False)
    # get_enrich_provider is imported inside the handler, so patch the source too.
    monkeypatch.setattr("app.dependencies.get_enrich_provider", lambda: FakeProvider())
    r = client.post("/nutrition/estimate", json={"name": "apple", "servings": 2}).json()
    assert r["ok"] is True
    assert r["estimate"]["calories"] == 190
