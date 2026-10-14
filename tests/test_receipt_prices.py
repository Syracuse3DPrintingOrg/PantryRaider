"""Receipt price capture (FoodAssistant-5osx).

Pure-logic coverage for services/receipt.py (prompt builder, tolerant reply
parser, name matcher, newest-entry pick) plus endpoint tests for
/receipt/analyze and /receipt/apply with a faked provider and a faked
GrocyClient, so nothing here touches a network or a real model. The analyze
endpoint acks instantly and reads in the background, so these tests poll
/receipt/status to the terminal state; the background-flow specifics (inbox
item, toast, dismiss, restart-lost) live in test_receipt_jobs.py.
"""
import io
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.providers.base import VisionProvider
from app.models.food import AnalysisResult
from app.services import receipt

_SERVICE_DIR = Path(__file__).parent.parent / "service"


# ── Prompt builder ───────────────────────────────────────────────────────────

def test_prices_prompt_asks_for_the_right_fields():
    prompt = receipt.build_prices_prompt()
    assert "JSON" in prompt
    assert '"price"' in prompt
    assert '"store"' in prompt
    assert '"quantity"' in prompt
    # The zero-price trap starts at extraction: an illegible price must come
    # back null, never 0.
    assert "null" in prompt


# ── Reply parser ─────────────────────────────────────────────────────────────

def test_parse_reply_fenced_json():
    raw = """```json
    {"store": "Wegmans", "items": [
      {"name": "Whole Milk", "price": "$3.49", "quantity": 1},
      {"name": "Eggs", "price": 4.29, "quantity": 2}
    ]}
    ```"""
    out = receipt.parse_receipt_reply(raw)
    assert out["store"] == "Wegmans"
    assert out["items"] == [
        {"name": "Whole Milk", "price": 3.49, "quantity": 1.0},
        {"name": "Eggs", "price": 4.29, "quantity": 2.0},
    ]


def test_parse_reply_junk_raises():
    with pytest.raises(ValueError):
        receipt.parse_receipt_reply("Sorry, I cannot read this image.")


def test_parse_reply_partial_rows():
    raw = """{"store": null, "items": [
      {"name": "Bread"},
      {"name": "", "price": 2.99},
      {"price": 1.99},
      {"name": "Freebie", "price": 0},
      {"name": "Refund", "price": -2.50},
      {"name": "Yogurt", "price": "junk", "quantity": "not a number"},
      "not even an object"
    ]}"""
    out = receipt.parse_receipt_reply(raw)
    assert out["store"] is None
    # Nameless and non-dict rows are dropped; bad prices become None (never
    # zero, never negative) and bad quantities fall back to 1.
    assert out["items"] == [
        {"name": "Bread", "price": None, "quantity": 1.0},
        {"name": "Freebie", "price": None, "quantity": 1.0},
        {"name": "Refund", "price": None, "quantity": 1.0},
        {"name": "Yogurt", "price": None, "quantity": 1.0},
    ]


def test_parse_reply_bare_list_and_unit_price_fallback():
    out = receipt.parse_receipt_reply(
        '[{"name": "Butter", "unit_price": 4.99}]')
    assert out["store"] is None
    assert out["items"] == [{"name": "Butter", "price": 4.99, "quantity": 1.0}]


def test_parse_reply_renamed_items_key():
    out = receipt.parse_receipt_reply(
        '{"store": "Aldi", "line_items": [{"name": "Salsa", "price": 2.19}]}')
    assert out["store"] == "Aldi"
    assert out["items"][0]["name"] == "Salsa"


# ── Matcher ──────────────────────────────────────────────────────────────────

_STOCK = [
    {"product_id": 5, "amount": 1, "product": {"name": "Whole Milk"}},
    {"product_id": 9, "amount": 2, "product": {"name": "Chicken Breast"}},
    {"product_id": 12, "amount": 6, "product": {"name": "Eggs"}},
]


def test_match_threshold_is_pinned():
    # The pairing bar the review UI relies on: below this a proposal is worse
    # than no proposal. Changing it is a deliberate decision, not a drive-by.
    assert receipt.MATCH_THRESHOLD == 0.6


def test_similarity_exact_and_fuzzy():
    assert receipt.similarity("Whole Milk", "Whole Milk") == 1.0
    # Receipt-style clipped spelling still lands on the product.
    assert receipt.similarity("Whole Mlk", "Whole Milk") >= receipt.MATCH_THRESHOLD
    assert receipt.similarity("Eggs", "Eggs Large Grade AA") >= receipt.MATCH_THRESHOLD


def test_similarity_rejects_lookalike_and_unrelated():
    # Shared first word is not enough: soup must not price the breast.
    assert receipt.similarity("Chicken Soup", "Chicken Breast") < receipt.MATCH_THRESHOLD
    assert receipt.similarity("Paper Towels", "Chicken Breast") < receipt.MATCH_THRESHOLD


def test_match_line_items_pairs_and_leaves_no_match():
    items = [
        {"name": "Whole Mlk Gal", "price": 3.49, "quantity": 1.0},
        {"name": "Paper Towels", "price": 5.99, "quantity": 1.0},
    ]
    out = receipt.match_line_items(items, _STOCK)
    assert out[0]["product_id"] == 5
    assert out[0]["product_name"] == "Whole Milk"
    assert out[0]["price"] == 3.49
    assert out[1]["product_id"] is None
    assert out[1]["product_name"] is None
    # Unmatched lines still come back so the review list can show them.
    assert out[1]["name"] == "Paper Towels"


def test_match_line_items_best_score_wins():
    stock = [
        {"product_id": 1, "amount": 3, "product": {"name": "Orange"}},
        {"product_id": 2, "amount": 1, "product": {"name": "Orange Juice"}},
    ]
    out = receipt.match_line_items(
        [{"name": "Orange Juice", "price": 4.79, "quantity": 1.0}], stock)
    assert out[0]["product_id"] == 2


def test_match_line_items_accepts_plain_product_rows():
    products = [{"id": 33, "name": "Sourdough Bread"}]
    out = receipt.match_line_items(
        [{"name": "Sourdough Bread", "price": 5.49, "quantity": 1.0}], products)
    assert out[0]["product_id"] == 33


# ── Newest entry pick ────────────────────────────────────────────────────────

def test_newest_entry_prefers_latest_creation():
    entries = [
        {"id": 1, "amount": 1, "row_created_timestamp": "2026-07-20 10:00:00"},
        {"id": 2, "amount": 1, "row_created_timestamp": "2026-07-22 09:00:00"},
        {"id": 3, "amount": 0, "row_created_timestamp": "2026-07-23 09:00:00"},
    ]
    # Entry 3 is newer but holds no stock; the receipt's add is entry 2.
    assert receipt.newest_entry(entries)["id"] == 2


def test_newest_entry_tie_breaks_on_row_id():
    ts = "2026-07-22 09:00:00"
    entries = [
        {"id": 7, "amount": 1, "row_created_timestamp": ts},
        {"id": 8, "amount": 1, "row_created_timestamp": ts},
    ]
    assert receipt.newest_entry(entries)["id"] == 8


def test_newest_entry_skips_unusable_rows():
    assert receipt.newest_entry([]) is None
    assert receipt.newest_entry(None) is None
    assert receipt.newest_entry([{"amount": 1}, "junk", {"id": "x", "amount": 1}]) is None


# ── Endpoints ────────────────────────────────────────────────────────────────

# Mutable holder so individual tests swap the canned model reply.
_PROVIDER_STATE = {"reply": ""}

_GOOD_REPLY = """```json
{"store": "Wegmans", "items": [
  {"name": "Whole Milk", "price": 3.49, "quantity": 1},
  {"name": "Paper Towels", "price": 5.99, "quantity": 1}
]}
```"""


class _FakeProvider(VisionProvider):
    async def analyze_food(self, image_data, mime_type):
        return AnalysisResult(items=[], image_type="food")

    async def analyze_receipt(self, image_data, mime_type):
        return AnalysisResult(items=[], image_type="receipt")

    async def extract_receipt_prices(self, image_data, mime_type):
        return _PROVIDER_STATE["reply"]

    async def health_check(self):
        return True


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), (220, 210, 200)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    cwd = os.getcwd()
    os.chdir(_SERVICE_DIR)
    try:
        from app.config import settings

        settings.data_dir = str(tmp_path_factory.mktemp("data"))

        from app.main import app
        from app.dependencies import get_vision_provider

        settings.grocy_base_url = "http://grocy.test"
        settings.grocy_api_key = "test-grocy-key"
        settings.vision_provider = "gemini"
        settings.gemini_api_key = "test-gemini-key"
        settings.auth_required = False
        settings.auth_password = ""

        app.dependency_overrides[get_vision_provider] = lambda: _FakeProvider()
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()
    finally:
        os.chdir(cwd)


@pytest.fixture(autouse=True)
def _mock_grocy(monkeypatch):
    """Fake Grocy: canned stock, canned entries, recorded price writes."""
    from app.services.grocy import GrocyClient, GrocyError

    written: list[tuple[int, float]] = []

    async def _stock(self):
        return _STOCK

    async def _get(self, path):
        assert path.startswith("/stock/products/")
        pid = int(path.split("/")[3])
        if pid == 5:
            return [
                {"id": 51, "amount": 1,
                 "row_created_timestamp": "2026-07-20 08:00:00"},
                {"id": 52, "amount": 1,
                 "row_created_timestamp": "2026-07-22 18:00:00"},
            ]
        if pid == 12:
            return [{"id": 120, "amount": 6,
                     "row_created_timestamp": "2026-07-22 18:00:00"}]
        return []

    async def _set_price(self, entry, price):
        if not entry.get("id") or not price or price <= 0:
            raise GrocyError("A real price and a stock entry id are required.")
        if entry["id"] == 120:
            raise GrocyError("Grocy 400 on /stock/entry/120: nope")
        written.append((entry["id"], price))
        return {}

    monkeypatch.setattr(GrocyClient, "get_stock", _stock)
    monkeypatch.setattr(GrocyClient, "_get", _get)
    monkeypatch.setattr(GrocyClient, "set_entry_price", _set_price)
    from app.services import usage
    monkeypatch.setattr(usage, "over_budget", lambda *a, **k: False)
    yield written


def _wait_status(client, want: str, timeout: float = 5.0) -> dict:
    """Poll /receipt/status until the background job reaches ``want``."""
    deadline = time.time() + timeout
    data = {}
    while time.time() < deadline:
        data = client.get("receipt/status").json()
        if data.get("status") == want:
            return data
        time.sleep(0.02)
    raise AssertionError(f"receipt status never became {want}: {data}")


def test_analyze_acks_instantly_and_result_waits(client, _mock_grocy):
    _PROVIDER_STATE["reply"] = _GOOD_REPLY
    files = {"file": ("receipt.png", _png_bytes(), "image/png")}
    r = client.post("receipt/analyze", files=files)
    # 202 with the user-forward "you can leave" message, never the result.
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "reading"
    assert "minute" in body["message"] and "leave this page" in body["message"]
    # The matches land in the stored slot for the page to pick up.
    data = _wait_status(client, "ready")
    assert data["store"] == "Wegmans"
    milk, towels = data["items"]
    assert milk["product_id"] == 5 and milk["price"] == 3.49
    assert towels["product_id"] is None
    assert data["priced"] == 1
    # Analyze is read-only: no price write happened.
    assert _mock_grocy == []
    # The result persists: a page revisit sees the same ready state.
    assert client.get("receipt/status").json()["status"] == "ready"


def test_analyze_rejects_non_image(client):
    files = {"file": ("receipt.txt", b"not an image", "text/plain")}
    assert client.post("receipt/analyze", files=files).status_code == 400


def test_analyze_junk_reply_fails_honestly(client):
    _PROVIDER_STATE["reply"] = "I could not find a receipt in this image."
    files = {"file": ("receipt.png", _png_bytes(), "image/png")}
    assert client.post("receipt/analyze", files=files).status_code == 202
    data = _wait_status(client, "failed")
    assert "could not be read" in data["message"]


def test_analyze_unsupported_provider_says_so(client):
    _PROVIDER_STATE["reply"] = None
    files = {"file": ("receipt.png", _png_bytes(), "image/png")}
    assert client.post("receipt/analyze", files=files).status_code == 202
    data = _wait_status(client, "failed")
    assert "Settings, AI" in data["message"]


def test_apply_writes_newest_entry_and_collects_failures(client, _mock_grocy):
    r = client.post("receipt/apply", json={"pairs": [
        {"product_id": 5, "price": 3.49, "name": "Whole Milk"},
        {"product_id": 12, "price": 4.29, "name": "Eggs"},
    ]})
    assert r.status_code == 200
    data = r.json()
    # Milk landed on the NEWEST entry (52, from the shopping trip), and the
    # Eggs failure was collected without sinking the batch.
    assert data["applied"] == 1
    assert _mock_grocy == [(52, 3.49)]
    assert data["failed"] == [{"name": "Eggs",
                              "reason": "Grocy 400 on /stock/entry/120: nope"}]


def test_apply_refuses_missing_price_per_item(client, _mock_grocy):
    r = client.post("receipt/apply", json={"pairs": [
        {"product_id": 5, "price": 0, "name": "Whole Milk"},
    ]})
    assert r.status_code == 200
    data = r.json()
    assert data["applied"] == 0
    assert _mock_grocy == []
    assert "No price" in data["failed"][0]["reason"]


def test_apply_reports_product_without_entries(client, _mock_grocy):
    r = client.post("receipt/apply", json={"pairs": [
        {"product_id": 9, "price": 7.99, "name": "Chicken Breast"},
    ]})
    assert r.status_code == 200
    data = r.json()
    assert data["applied"] == 0
    assert "No stock entry" in data["failed"][0]["reason"]
