"""Calories on consume (FoodAssistant-4mi3).

A consume-mode scan whose product has real calorie data in Open Food Facts
offers a one-tap "Log as eaten" for the nutrition journal. Covers the pure
offer builder's truth table (data present -> offer shape, absent -> no offer,
never a zero-calorie guess), the additive scan-reply wiring, and the existing
/nutrition/log endpoint the offer posts to. No network: Grocy and the OFF
fetch are stubbed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import nutrition, scanner_mode  # noqa: E402


# -- calorie_offer truth table ----------------------------------------------

def _product(**kw):
    p = {"nutriments": {}}
    p.update(kw)
    return p


def test_per_serving_calories_win():
    offer = nutrition.calorie_offer("Yogurt Cup", _product(
        serving_size="150 g",
        nutriments={"energy-kcal_serving": 130, "proteins_serving": 5.2,
                    "carbohydrates_serving": 18, "fat_serving": 3.5,
                    "energy-kcal_100g": 87},
    ))
    assert offer == {"name": "Yogurt Cup", "calories": 130, "protein": 5.2,
                     "carbs": 18, "fat": 3.5, "basis": "per serving (150 g)"}


def test_per_100g_scaled_by_serving_weight():
    offer = nutrition.calorie_offer("Crackers", _product(
        serving_size="30 g", serving_quantity="30",
        nutriments={"energy-kcal_100g": 500, "proteins_100g": 10,
                    "fat_100g": 20},
    ))
    assert offer["calories"] == 150.0
    assert offer["protein"] == 3.0 and offer["fat"] == 6.0
    assert offer["carbs"] is None          # unknown stays unknown, never 0
    assert offer["basis"] == "per serving (30 g)"


def test_per_100g_alone_is_offered_and_labeled():
    offer = nutrition.calorie_offer("Peanut Butter", _product(
        nutriments={"energy-kcal_100g": 588},
    ))
    assert offer["calories"] == 588
    assert offer["basis"] == "per 100 g"


def test_genuine_zero_calories_is_data_not_a_guess():
    offer = nutrition.calorie_offer("Diet Cola", _product(
        serving_size="355 ml",
        nutriments={"energy-kcal_serving": 0},
    ))
    assert offer is not None and offer["calories"] == 0


def test_no_calorie_data_means_no_offer():
    # Macros without calories, empty nutriments, junk values, no name, and a
    # non-dict product all yield None: nothing is ever invented.
    assert nutrition.calorie_offer("Mystery", _product(
        nutriments={"proteins_100g": 9})) is None
    assert nutrition.calorie_offer("Mystery", _product()) is None
    assert nutrition.calorie_offer("Mystery", _product(
        nutriments={"energy-kcal_serving": "n/a"})) is None
    assert nutrition.calorie_offer("", _product(
        nutriments={"energy-kcal_serving": 100})) is None
    assert nutrition.calorie_offer("Mystery", None) is None
    assert nutrition.calorie_offer("Mystery", {"nutriments": "junk"}) is None


# -- scan-reply wiring + log endpoint ---------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    scanner_mode.reset()
    try:
        yield TestClient(app)
    finally:
        scanner_mode.reset()
        os.chdir(cwd)


@pytest.fixture(autouse=True)
def _clean_intake():
    from app.database import SessionLocal
    from app.models.db_models import IntakeLog
    db = SessionLocal()
    db.query(IntakeLog).delete()
    db.commit()
    db.close()
    yield
    db = SessionLocal()
    db.query(IntakeLog).delete()
    db.commit()
    db.close()


def _stub_consume_ok(monkeypatch):
    from app.services.grocy import GrocyClient

    async def _consume(self, barcode, amount=1.0):
        return {"ok": True}

    monkeypatch.setattr(GrocyClient, "consume_by_barcode", _consume)


def _stub_off(monkeypatch, product):
    from app.routers import pending as pending_router

    async def _fetch(barcode):
        return product

    monkeypatch.setattr(pending_router, "fetch_off_product", _fetch)


def test_consume_reply_carries_the_offer_and_it_logs(client, monkeypatch):
    scanner_mode.set_mode("consume")
    _stub_consume_ok(monkeypatch)
    _stub_off(monkeypatch, {
        "product_name": "Greek Yogurt", "brands": "Chobani",
        "serving_size": "150 g",
        "nutriments": {"energy-kcal_serving": 120, "proteins_serving": 12},
    })
    r = client.post("/pending/scan", json={"barcode": "894700010137"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "consumed"
    # The OFF display name rides along so the reply reads like a product.
    assert body["name"] == "Chobani Greek Yogurt"
    offer = body["log_offer"]
    assert offer["calories"] == 120 and offer["protein"] == 12
    assert offer["basis"] == "per serving (150 g)"

    # One tap: the kiosk posts the offer to the existing intake endpoint.
    logged = client.post("/nutrition/log", json={
        "name": offer["name"], "servings": 1, "calories": offer["calories"],
        "protein": offer["protein"], "carbs": offer["carbs"],
        "fat": offer["fat"], "source": "barcode",
    })
    assert logged.status_code == 200
    day = client.get("/nutrition/today").json()
    assert day["totals"]["calories"] == 120
    assert day["entries"][0]["name"] == "Chobani Greek Yogurt"
    assert day["entries"][0]["source"] == "barcode"


def test_no_calorie_data_no_offer_in_reply(client, monkeypatch):
    scanner_mode.set_mode("consume")
    _stub_consume_ok(monkeypatch)
    _stub_off(monkeypatch, {"product_name": "Mystery Snack",
                            "nutriments": {}})
    body = client.post("/pending/scan", json={"barcode": "012345678905"}).json()
    assert body["status"] == "consumed"
    assert "log_offer" not in body


def test_off_outage_never_blocks_the_consume(client, monkeypatch):
    scanner_mode.set_mode("consume")
    _stub_consume_ok(monkeypatch)
    from app.routers import pending as pending_router

    async def _boom(barcode):
        raise RuntimeError("OFF down")

    monkeypatch.setattr(pending_router, "fetch_off_product", _boom)
    body = client.post("/pending/scan", json={"barcode": "012345678905"}).json()
    assert body["status"] == "consumed"
    assert "log_offer" not in body
