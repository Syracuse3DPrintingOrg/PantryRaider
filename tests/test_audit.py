"""Tests for pantry audit mode (FoodAssistant-ugku).

Covers the pure in-memory session logic (start/record/status/stop, matching,
missing/unexpected) and the /audit endpoints via TestClient with Grocy stock
mocked, plus the scanner-mode "audit" dispatch in /pending/scan. The audit is
read only: no test asserts any Grocy write, because the code never makes one.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import audit  # noqa: E402
from app.services import scanner_mode  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    audit.reset()
    scanner_mode.reset()
    yield
    audit.reset()
    scanner_mode.reset()


# Pure session logic --------------------------------------------------------

def test_no_session_by_default():
    assert audit.is_active() is False
    s = audit.status()
    assert s["active"] is False
    assert s["expected"] == [] and s["scanned"] == []


def test_record_scan_without_session_raises():
    with pytest.raises(RuntimeError):
        audit.record_scan("Milk")


def test_normalize_loose_match():
    assert audit.normalize("  Whole MILK! ") == "whole milk"
    assert audit.normalize("Ground-Beef") == "ground beef"


def test_start_snapshots_expected_and_status():
    audit.start("Fridge", [{"name": "Milk", "amount": 2}, {"name": "Eggs", "amount": 12}])
    assert audit.is_active() is True
    assert audit.get_location() == "Fridge"
    s = audit.status()
    assert s["counts"]["expected"] == 2
    assert s["counts"]["seen"] == 0
    assert set(s["missing"]) == {"Milk", "Eggs"}


def test_scan_matches_expected_item_case_insensitive():
    audit.start("Fridge", [{"name": "Whole Milk"}])
    res = audit.record_scan("whole milk")
    assert res["status"] == "matched"
    s = audit.status()
    assert s["counts"]["seen"] == 1
    assert s["missing"] == []
    seen = [e for e in s["expected"] if e["seen"]]
    assert seen[0]["scanned_count"] == 1


def test_repeat_scan_bumps_count():
    audit.start("Fridge", [{"name": "Milk"}])
    audit.record_scan("Milk")
    res = audit.record_scan("Milk")
    assert res["count"] == 2
    assert len(audit.status()["scanned"]) == 1


def test_unexpected_scan_flagged():
    audit.start("Fridge", [{"name": "Milk"}])
    res = audit.record_scan("Ketchup")
    assert res["status"] == "unexpected"
    s = audit.status()
    assert s["unexpected"] == ["Ketchup"]
    assert s["counts"]["unexpected"] == 1
    # Milk was never scanned, so it stays missing.
    assert s["missing"] == ["Milk"]


def test_stop_returns_final_then_clears():
    audit.start("Pantry", [{"name": "Rice"}])
    audit.record_scan("Rice")
    final = audit.stop()
    assert final["counts"]["seen"] == 1
    assert audit.is_active() is False


def test_start_replaces_previous_session():
    audit.start("Fridge", [{"name": "Milk"}])
    audit.record_scan("Milk")
    audit.start("Pantry", [{"name": "Rice"}])
    assert audit.get_location() == "Pantry"
    assert audit.status()["scanned"] == []


# Endpoints -----------------------------------------------------------------

_STOCK = [
    {"name": "Milk", "amount": 2, "days_remaining": 5, "location_name": "Fridge", "storage_bucket": "fridge"},
    {"name": "Eggs", "amount": 12, "days_remaining": 10, "location_name": "Fridge", "storage_bucket": "fridge"},
    {"name": "Rice", "amount": 1, "days_remaining": 300, "location_name": "Pantry", "storage_bucket": "pantry"},
]


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
    monkeypatch.setattr(settings, "vision_provider", "gemini", raising=False)
    monkeypatch.setattr(settings, "gemini_api_key", "k", raising=False)

    from app.services.grocy import GrocyClient

    async def _stock(self):
        return list(_STOCK)

    monkeypatch.setattr(GrocyClient, "get_full_stock", _stock)

    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_locations_endpoint_lists_stocked_locations(client):
    data = client.get("/audit/locations").json()
    names = {l["name"]: l["item_count"] for l in data["locations"]}
    assert names == {"Fridge": 2, "Pantry": 1}


def test_start_status_scan_flow(client, monkeypatch):
    started = client.post("/audit/start", json={"location": "Fridge"}).json()
    assert started["location"] == "Fridge"
    assert started["counts"]["expected"] == 2

    # Scan by explicit name (matches Milk).
    r = client.post("/audit/scan", json={"name": "Milk"})
    assert r.json()["status"] == "matched"

    s = client.get("/audit/status").json()
    assert s["counts"]["seen"] == 1
    assert s["missing"] == ["Eggs"]


def test_scan_resolves_barcode_to_name(client, monkeypatch):
    client.post("/audit/start", json={"location": "Fridge"})
    from app.routers import audit as audit_router

    async def _lookup(barcode, db):
        from app.models.food import FoodItem
        return FoodItem(name="Eggs")

    monkeypatch.setattr(audit_router, "lookup_barcode", _lookup)
    r = client.post("/audit/scan", json={"barcode": "111"})
    assert r.json()["status"] == "matched"
    assert client.get("/audit/status").json()["missing"] == ["Milk"]


def test_scan_without_session_returns_status_not_error(client):
    r = client.post("/audit/scan", json={"name": "Milk"})
    assert r.status_code == 200
    assert r.json()["status"] == "no_session"


def test_stop_endpoint(client):
    client.post("/audit/start", json={"location": "Pantry"})
    client.post("/audit/scan", json={"name": "Rice"})
    final = client.post("/audit/stop").json()
    assert final["counts"]["seen"] == 1
    assert client.get("/audit/status").json()["active"] is False


def test_pending_scan_audit_mode_records_not_queues(client, monkeypatch):
    """In scanner-mode audit, /pending/scan records the scan against the audit
    session and never queues a pending row or writes to Grocy."""
    client.post("/audit/start", json={"location": "Fridge"})
    scanner_mode.set_mode("audit")

    from app.routers import pending as pending_router

    async def _lookup(barcode, db):
        from app.models.food import FoodItem
        return FoodItem(name="Milk")

    monkeypatch.setattr(pending_router, "lookup_barcode", _lookup)
    before = client.get("/pending/count").json()["count"]
    r = client.post("/pending/scan", json={"barcode": "222"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "audit"
    assert body["status"] == "matched"
    # No pending row was created (audit is read only).
    assert client.get("/pending/count").json()["count"] == before
    # And the audit session saw it.
    assert client.get("/audit/status").json()["counts"]["seen"] == 1


def test_pending_scan_audit_mode_without_session(client):
    scanner_mode.set_mode("audit")
    r = client.post("/pending/scan", json={"barcode": "333"})
    assert r.status_code == 200
    assert r.json()["status"] == "no_audit_session"
