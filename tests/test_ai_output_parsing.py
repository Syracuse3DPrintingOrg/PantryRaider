"""AI reply parsing is total: one bad field or row never fails a whole photo or
receipt, and non-finite numbers never get through.

A model's reply is untrusted input. Before this, _parse_item handed model
values straight to FoodItem, so a confidence of 90, a best-by date written as
printed on the label, a null name, or a bare string row raised a validation
error, and because a receipt's rows were built in one loop, a single bad row
lost the whole receipt. A JSON array reply to the food prompt raised
AttributeError. json.loads reads 1e999 as infinity, which was accepted as a
quantity or a price and then broke the JSON response the page reads.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import date
from pathlib import Path

import httpx
import pytest

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

# ollama.py takes its prompts from gemini.py, which imports the Gemini SDK at
# module load; the SDK is not installed in the pure-logic test environment.
# Stub it the same way tests/test_receipt_purchase.py does.
if "google.generativeai" not in sys.modules:
    _google = sys.modules.setdefault("google", types.ModuleType("google"))
    _genai = types.ModuleType("google.generativeai")
    _genai.configure = lambda **kwargs: None
    _genai.GenerativeModel = lambda *args, **kwargs: types.SimpleNamespace()
    _google.generativeai = _genai
    sys.modules["google.generativeai"] = _genai

from app.providers import base  # noqa: E402
from app.providers.base import (_parse_item, _parse_receipt, _safe_date,  # noqa: E402
                                nutrition_fields)
from app.services.receipt import _to_price, _to_quantity, parse_receipt_reply  # noqa: E402
from app.models.food import FoodCategory, StorageType  # noqa: E402


def _item(reply, default=0.8):
    """Parse a raw JSON reply the way every provider does."""
    return _parse_item(base.parse_json_response(reply), default_confidence=default)


def _json_safe(model) -> str:
    """The model as strict JSON (no Infinity or NaN), the way the page gets it."""
    return json.dumps(model.model_dump(mode="json"), allow_nan=False)


# --- one item ------------------------------------------------------------------

def test_confidence_given_as_a_percentage_is_scaled():
    assert _item('{"name": "Milk", "confidence": 90}').confidence == pytest.approx(0.9)
    assert _item('{"name": "Milk", "confidence": "85%"}').confidence == pytest.approx(0.85)


def test_confidence_out_of_range_or_unreadable_is_clamped_or_defaulted():
    assert _item('{"name": "Milk", "confidence": 250}').confidence == 1.0
    assert _item('{"name": "Milk", "confidence": -0.4}').confidence == 0.0
    assert _item('{"name": "Milk", "confidence": "high"}', 0.75).confidence == 0.75
    assert _item('{"name": "Milk", "confidence": null}', 0.75).confidence == 0.75
    assert _item('{"name": "Milk", "confidence": 1e999}', 0.75).confidence == 0.75


def test_non_iso_best_by_date_never_fails_the_item():
    item = _item('{"name": "Yogurt", "best_by_date": "03/15/2027"}')
    assert item.name == "Yogurt"
    assert item.best_by_date in (date(2027, 3, 15), None)


def test_non_iso_best_by_date_is_read_when_it_has_a_four_digit_year():
    pytest.importorskip("dateutil")
    assert _item('{"name": "Y", "best_by_date": "03/15/2027"}').best_by_date == date(2027, 3, 15)
    assert _safe_date("March 15, 2027") == date(2027, 3, 15)
    assert _safe_date("15/03/2027") == date(2027, 3, 15)
    # A month and year with no day reads as the first of the month.
    assert _safe_date("03/2027") == date(2027, 3, 1)
    assert _safe_date("2027-03-15T00:00:00Z") == date(2027, 3, 15)


def test_dates_that_cannot_be_read_are_none():
    assert _safe_date("2027-03-15") == date(2027, 3, 15)
    for junk in ("03/15/27", "2027", "best by 2027", "soon", "", None, 20270315,
                 ["2027-03-15"], {"d": 1}):
        assert _safe_date(junk) is None, junk
    assert _item('{"name": "Y", "best_by_date": "03/15/27"}').best_by_date is None


def test_null_or_blank_name_becomes_unknown():
    assert _item('{"name": null, "quantity": 2}').name == "Unknown"
    assert _item('{"name": "   "}').name == "Unknown"
    assert _item('{"name": ["Milk"]}').name == "Unknown"
    assert _item('{"quantity": 2}').name == "Unknown"


def test_name_is_trimmed_and_capped():
    assert _item('{"name": "  Roma tomatoes  "}').name == "Roma tomatoes"
    long_name = "Cheddar " * 40
    assert len(_item(json.dumps({"name": long_name})).name) <= 120
    assert _item('{"name": 7}').name == "7"


def test_quantity_1e999_is_rejected():
    item = _item('{"name": "Rice", "quantity": 1e999}')
    assert item.quantity == 1.0
    _json_safe(item)


def test_negative_zero_and_absurd_quantities_fall_back_to_one():
    for q in ("-2", "0", "20000", '"lots"', "null", "true", '"nan"'):
        assert _item('{"name": "Rice", "quantity": %s}' % q).quantity == 1.0, q
    assert _item('{"name": "Rice", "quantity": "2.5"}').quantity == 2.5
    assert _item('{"name": "Rice", "quantity": 10000}').quantity == 10000


def test_unit_brand_notes_are_strings_or_defaults():
    item = _item('{"name": "Milk", "unit": 5, "brand": ["x"], "notes": {"a": 1}}')
    assert item.unit == "item"
    assert item.brand is None
    assert item.notes is None
    item = _item(json.dumps({"name": "Milk", "unit": " gallon ", "brand": " Wegmans ",
                             "notes": "n" * 2000}))
    assert item.unit == "gallon"
    assert item.brand == "Wegmans"
    assert len(item.notes) <= 500


def test_storage_and_category_match_without_regard_to_case():
    item = _item('{"name": "Ice cream", "storage_type": "Frozen", "category": "dairy"}')
    assert item.storage_type == StorageType.frozen
    assert item.category == FoodCategory.dairy
    item = _item('{"name": "Bread", "storage_type": "Room temp", "category": 3}')
    assert item.storage_type == StorageType.room_temp
    assert item.category == FoodCategory.other


def test_food_reply_that_is_a_json_array_uses_its_first_object():
    item = _item('```json\n[{"name": "Roma tomatoes", "quantity": 3},'
                 ' {"name": "Yellow onion"}]\n```')
    assert item.name == "Roma tomatoes"
    assert item.quantity == 3


def test_any_reply_shape_gives_an_item():
    for reply in ("[]", '["Milk"]', '"Milk"', "42", "null"):
        assert _item(reply).name == "Unknown", reply


def test_ollama_food_reply_that_is_an_array_is_not_an_error(monkeypatch, tmp_path):
    # The whole provider path: this raised AttributeError ('list' object has
    # no attribute 'get') before.
    from app.config import settings
    from app.providers.ollama import OllamaProvider
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))

    def handler(request):
        return httpx.Response(200, json={
            "response": '[{"name": "Sharp cheddar", "confidence": 88}]'})

    provider = OllamaProvider("http://ollama.test", transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.analyze_food(b"img", "image/jpeg"))
    assert result.items[0].name == "Sharp cheddar"
    assert result.items[0].confidence == pytest.approx(0.88)


# --- receipts ------------------------------------------------------------------

def test_receipt_with_one_bad_row_keeps_the_good_rows():
    reply = json.dumps({
        "store": "Wegmans", "purchase_date": "2026-09-20",
        "items": [
            {"name": "Milk", "quantity": 1},
            {"name": None, "quantity": -3, "confidence": 90,
             "best_by_date": "someday", "storage_type": ["cold"]},
            {"name": "Eggs", "quantity": 12, "confidence": 0.9},
        ],
    })
    result = _parse_receipt(base.parse_json_response(reply), 0.8, raw=reply)
    assert [i.name for i in result.items] == ["Milk", "Unknown", "Eggs"]
    assert result.items[1].quantity == 1.0
    assert result.store == "Wegmans"
    assert all(i.purchased_on == date(2026, 9, 20) for i in result.items)
    _json_safe(result)


def test_receipt_items_given_as_strings_are_skipped_not_fatal():
    data = {"store": "Aldi", "items": ["Milk", "TAX 0.53",
                                       {"name": "Bread", "quantity": 1}, 7, None]}
    result = _parse_receipt(data, 0.8, raw="{}")
    assert [i.name for i in result.items] == ["Bread"]
    assert result.store == "Aldi"


def test_receipt_row_that_still_fails_is_dropped_alone(monkeypatch):
    real = base._parse_item

    def flaky(row, default_confidence):
        if row.get("name") == "Boom":
            raise RuntimeError("unexpected")
        return real(row, default_confidence=default_confidence)

    monkeypatch.setattr(base, "_parse_item", flaky)
    data = [{"name": "Milk"}, {"name": "Boom"}, {"name": "Eggs"}]
    result = _parse_receipt(data, 0.8, raw="[]")
    assert [i.name for i in result.items] == ["Milk", "Eggs"]


def test_receipt_reply_of_any_shape_gives_a_result():
    for data in ("text", 42, None, {"items": None, "store": 5}, {"items": "none"}):
        result = _parse_receipt(data, 0.8, raw="x")
        assert result.items == [], data
        assert result.store is None


def test_receipt_items_given_as_one_object_are_read():
    result = _parse_receipt({"items": {"name": "Milk"}}, 0.8, raw="{}")
    assert [i.name for i in result.items] == ["Milk"]


def test_receipt_quantity_1e999_is_rejected():
    reply = '{"items": [{"name": "Milk", "quantity": 1e999}]}'
    result = _parse_receipt(base.parse_json_response(reply), 0.8, raw=reply)
    assert result.items[0].quantity == 1.0
    _json_safe(result)


# --- receipt price capture -----------------------------------------------------

def test_receipt_prices_that_are_not_finite_are_dropped():
    reply = ('{"store": "Wegmans", "items": ['
             '{"name": "Milk", "price": "inf", "quantity": 1},'
             '{"name": "Eggs", "price": 1e999, "quantity": 1e999},'
             '{"name": "Bread", "price": "nan", "quantity": "-inf"},'
             '{"name": "Butter", "price": 4.29, "quantity": 2}]}')
    parsed = parse_receipt_reply(reply)
    prices = {row["name"]: row["price"] for row in parsed["items"]}
    assert prices == {"Milk": None, "Eggs": None, "Bread": None, "Butter": 4.29}
    quantities = {row["name"]: row["quantity"] for row in parsed["items"]}
    assert quantities["Eggs"] == 1.0 and quantities["Bread"] == 1.0
    assert quantities["Butter"] == 2
    json.dumps(parsed, allow_nan=False)


def test_price_and_quantity_bounds():
    assert _to_price("-inf") is None
    assert _to_price(10000) is None
    assert _to_price("9999.99") == 9999.99
    assert _to_price(0.004) is None  # rounds to 0.00, which is never kept
    assert _to_price(True) is None
    assert _to_price("$3.49") == 3.49
    assert _to_quantity(10000) == 1.0
    assert _to_quantity(9999) == 9999
    assert _to_quantity(float("nan")) == 1.0
    assert _to_quantity(False) == 1.0


# --- nutrition estimates -------------------------------------------------------

def test_nutrition_estimate_drops_non_finite_numbers():
    out = nutrition_fields(json.loads(
        '{"calories": 1e999, "protein": "12.34", "carbs": "nan", "fat": null}'))
    assert out == {"calories": None, "protein": 12.3, "carbs": None, "fat": None}
    json.dumps(out, allow_nan=False)


# --- one implementation --------------------------------------------------------

def test_every_provider_uses_the_same_parsers():
    from app.providers import cloud, gemini, ollama
    for name in ("_parse_item", "_parse_receipt", "_safe_date", "_safe_storage",
                 "_safe_category"):
        assert getattr(gemini, name) is getattr(base, name), name
    assert ollama._parse_item is base._parse_item
    assert cloud._parse_item is base._parse_item
    assert cloud._parse_receipt is base._parse_receipt
