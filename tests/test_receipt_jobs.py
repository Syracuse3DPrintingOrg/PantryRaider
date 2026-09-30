"""Background receipt reading: instant ack, stored results, inbox hand-off.

Covers services/receipt_jobs.py (the receipt_results.json state round-trip:
pending/ready/failed shapes, supersede rules, the restart-lost conversion)
and the endpoint flow in routers/receipt.py with a monkeypatched provider
that resolves later via asyncio: analyze acks while the read is still
running, the finished result raises a Review inbox action item and a kiosk
toast, dismiss archives it, apply resolves it. No network, no real model.
"""
import asyncio
import io
import json
import os
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.models.food import AnalysisResult
from app.providers.base import VisionProvider
from app.services import receipt_jobs

_SERVICE_DIR = Path(__file__).parent.parent / "service"


# ── Pure state round-trip ────────────────────────────────────────────────────

_ITEMS = [
    {"name": "Whole Milk", "price": 3.49, "quantity": 1.0,
     "product_id": 5, "product_name": "Whole Milk", "score": 1.0},
    {"name": "Paper Towels", "price": 5.99, "quantity": 1.0,
     "product_id": None, "product_name": None, "score": 0.1},
    {"name": "Bread", "price": None, "quantity": 1.0,
     "product_id": 7, "product_name": "Bread", "score": 1.0},
]


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    receipt_jobs.reset()
    yield tmp_path
    receipt_jobs.reset()


def test_state_round_trip_ready(state_dir):
    assert receipt_jobs.get_status() == {"status": "none"}
    job = receipt_jobs.start()
    status = receipt_jobs.get_status()
    assert status["status"] == "pending" and status["job_id"] == job
    assert receipt_jobs.mark_ready(job, "Wegmans", _ITEMS, action_item_id=7)
    status = receipt_jobs.get_status()
    assert status["status"] == "ready"
    assert status["store"] == "Wegmans"
    assert status["items"] == _ITEMS
    # Only lines with both a price and a matched product count as confirmable.
    assert status["priced"] == 1
    assert status["action_item_id"] == 7
    # The result survives on disk exactly as reported.
    on_disk = json.loads((state_dir / "receipt_results.json").read_text())
    assert on_disk["status"] == "ready" and on_disk["items"] == _ITEMS
    # clear() hands back the payload (for retiring the inbox item) and empties.
    prev = receipt_jobs.clear()
    assert prev["action_item_id"] == 7
    assert receipt_jobs.get_status() == {"status": "none"}
    assert not (state_dir / "receipt_results.json").exists()


def test_state_round_trip_failed(state_dir):
    job = receipt_jobs.start()
    assert receipt_jobs.mark_failed(job, "The receipt could not be read.",
                                    action_item_id=9)
    status = receipt_jobs.get_status()
    assert status["status"] == "failed"
    assert status["message"] == "The receipt could not be read."
    assert status["action_item_id"] == 9


def test_superseded_job_result_is_dropped(state_dir):
    old = receipt_jobs.start()
    new = receipt_jobs.start()
    # The old job finishing late must not clobber the new job's slot.
    assert not receipt_jobs.mark_ready(old, None, _ITEMS)
    status = receipt_jobs.get_status()
    assert status["status"] == "pending" and status["job_id"] == new
    assert receipt_jobs.mark_ready(new, None, _ITEMS)
    assert receipt_jobs.get_status()["status"] == "ready"


def test_restart_mid_job_degrades_to_lost(state_dir):
    receipt_jobs.start()
    # Simulate the app process dying mid-read: the pending state is on disk
    # but no process is running the job anymore.
    receipt_jobs._running.clear()
    status = receipt_jobs.get_status()
    assert status["status"] == "failed"
    assert "lost" in status["message"]
    # The honest verdict is persisted, not re-derived: it stays failed.
    assert receipt_jobs.get_status()["status"] == "failed"
    on_disk = json.loads((state_dir / "receipt_results.json").read_text())
    assert on_disk["status"] == "failed"


def test_priced_count_shapes():
    assert receipt_jobs.priced_count(None) == 0
    assert receipt_jobs.priced_count([]) == 0
    assert receipt_jobs.priced_count(_ITEMS) == 1
    assert receipt_jobs.priced_count(["junk", {"price": 2.0}]) == 0


# ── Endpoint flow with a provider that resolves later ────────────────────────

_GOOD_REPLY = """```json
{"store": "Wegmans", "items": [
  {"name": "Whole Milk", "price": 3.49, "quantity": 1},
  {"name": "Paper Towels", "price": 5.99, "quantity": 1}
]}
```"""

_STOCK = [
    {"product_id": 5, "amount": 1, "product": {"name": "Whole Milk"}},
]

_PROVIDER = {"instance": None}


class _SlowProvider(VisionProvider):
    """A provider whose read finishes only when the test releases it, so the
    ack demonstrably returns while the read is still in flight."""

    def __init__(self):
        self.release = threading.Event()
        self.reply = _GOOD_REPLY
        self.exc = None

    async def analyze_food(self, image_data, mime_type):
        return AnalysisResult(items=[], image_type="food")

    async def analyze_receipt(self, image_data, mime_type):
        return AnalysisResult(items=[], image_type="receipt")

    async def extract_receipt_prices(self, image_data, mime_type):
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        if self.exc is not None:
            raise self.exc
        return self.reply

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

        app.dependency_overrides[get_vision_provider] = \
            lambda: _PROVIDER["instance"]
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()
    finally:
        os.chdir(cwd)


@pytest.fixture
def provider(client, monkeypatch):
    """A fresh slow provider plus a clean slot, fake Grocy, and quiet budget."""
    from app.services import ha_events, usage
    from app.services.grocy import GrocyClient, GrocyError

    slow = _SlowProvider()
    _PROVIDER["instance"] = slow
    receipt_jobs.reset()
    ha_events.reset()

    async def _stock(self):
        return _STOCK

    async def _entries(self, path):
        assert path.startswith("/stock/products/")
        return [{"id": 52, "amount": 1,
                 "row_created_timestamp": "2026-07-28 18:00:00"}]

    async def _set_price(self, entry, price):
        if not entry.get("id") or not price or price <= 0:
            raise GrocyError("A real price and a stock entry id are required.")
        return {}

    monkeypatch.setattr(GrocyClient, "get_stock", _stock)
    monkeypatch.setattr(GrocyClient, "_get", _entries)
    monkeypatch.setattr(GrocyClient, "set_entry_price", _set_price)
    monkeypatch.setattr(usage, "over_budget", lambda *a, **k: False)
    yield slow
    # Release any still-parked read and give the app loop a beat to finish it,
    # THEN drop the slot: this keeps task cancellation on the app's own loop
    # (reset() from the test thread must never cancel a live asyncio task).
    slow.release.set()
    time.sleep(0.05)
    receipt_jobs.reset()


def _post_receipt(client):
    files = {"file": ("receipt.png", _png_bytes(), "image/png")}
    return client.post("receipt/analyze", files=files)


def _wait_status(client, want: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    data = {}
    while time.time() < deadline:
        data = client.get("receipt/status").json()
        if data.get("status") == want:
            return data
        time.sleep(0.02)
    raise AssertionError(f"receipt status never became {want}: {data}")


def _item(item_id: int) -> dict | None:
    from app.database import SessionLocal
    from app.services import action_items
    db = SessionLocal()
    try:
        return action_items.get(db, item_id)
    finally:
        db.close()


def test_completion_raises_inbox_item_and_toast(client, provider):
    r = _post_receipt(client)
    assert r.status_code == 202
    assert r.json()["status"] == "reading"
    # The read is still in flight: the slot says pending, with the same
    # user-forward message the ack carried.
    status = client.get("receipt/status").json()
    assert status["status"] == "pending"
    assert "Review inbox" in status["message"]
    # Now the model answers.
    provider.release.set()
    data = _wait_status(client, "ready")
    assert data["priced"] == 1
    # An action item landed in the inbox, deep-linking to the Shopping page.
    item = _item(data["action_item_id"])
    assert item is not None
    assert item["title"] == "Receipt read: 1 price ready to confirm"
    assert item["status"] == "open"
    assert item["level"] == "success"
    assert item["payload"]["url"] == "ui/shopping"
    assert "Wegmans" in item["body"]
    # And the kiosk got a toast (never a forced navigation).
    from app.services import ha_events
    events = ha_events.poll(0)["events"]
    assert any(e["type"] == "notification" and e["title"] == "Receipt read"
               for e in events)
    assert not any(e["type"] == "navigate" for e in events)


def test_failure_is_honest_and_dismiss_archives(client, provider):
    provider.exc = RuntimeError("provider down")
    assert _post_receipt(client).status_code == 202
    provider.release.set()
    data = _wait_status(client, "failed")
    assert "could not be read" in data["message"]
    item = _item(data["action_item_id"])
    assert item["title"] == "Receipt could not be read"
    assert item["status"] == "open" and item["level"] == "warning"
    # Dismissing clears the stored result and retires the inbox item.
    assert client.post("receipt/dismiss").json()["ok"] is True
    assert client.get("receipt/status").json()["status"] == "none"
    assert _item(data["action_item_id"])["status"] == "archived"


def test_apply_clears_slot_and_resolves_item(client, provider):
    provider.release.set()
    assert _post_receipt(client).status_code == 202
    data = _wait_status(client, "ready")
    r = client.post("receipt/apply", json={"pairs": [
        {"product_id": 5, "price": 3.49, "name": "Whole Milk"},
    ]})
    assert r.status_code == 200 and r.json()["applied"] == 1
    # The receipt is handled: the slot empties and the inbox item resolves.
    assert client.get("receipt/status").json()["status"] == "none"
    assert _item(data["action_item_id"])["status"] == "done"


def test_new_upload_supersedes_and_retires_old_item(client, provider):
    provider.release.set()
    assert _post_receipt(client).status_code == 202
    old = _wait_status(client, "ready")
    # A second receipt takes the slot; the old unconfirmed item is archived
    # so the inbox never piles up stale "ready to confirm" notes.
    assert _post_receipt(client).status_code == 202
    new = _wait_status(client, "ready")
    assert new["action_item_id"] != old["action_item_id"]
    assert _item(old["action_item_id"])["status"] == "archived"
    assert _item(new["action_item_id"])["status"] == "open"


def test_restart_mid_read_reports_lost_via_endpoint(client, provider):
    assert _post_receipt(client).status_code == 202
    assert client.get("receipt/status").json()["status"] == "pending"
    # Simulate the process dying mid-read (the state file survives, the
    # in-process job registry does not).
    receipt_jobs._running.clear()
    data = client.get("receipt/status").json()
    assert data["status"] == "failed"
    assert "lost" in data["message"]
