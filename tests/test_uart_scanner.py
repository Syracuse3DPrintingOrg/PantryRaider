"""UART barcode scanner: scan session, fast-ack background enrichment, and the
/gadgets/config contract (FoodAssistant-x61t).

Covers:
  - the scan-session presence flag: inactive until a ping, active for a short
    TTL after one, expiring on its own, and shared across workers;
  - the fast-ack scan path: an inventory scan returns immediately with a queued
    placeholder (no synchronous Open Food Facts / LLM call) and schedules the
    name lookup, which a background task then fills in on the row;
  - GET /gadgets/config carrying the scanner_uart block the host reader reads;
  - the scanner_uart settings plumbing;
  - the Manage Pantry page rendering the scan-session heartbeat hooks.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import scan_session, scanner_mode  # noqa: E402


# -- scan session TTL (pure) --------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_session(monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    scan_session.reset()
    yield
    scan_session.reset()


def test_session_inactive_before_any_ping():
    assert scan_session.is_active() is False
    assert scan_session.state()["active"] is False


def test_ping_makes_session_active_then_expires():
    scan_session.ping(now=100.0)
    assert scan_session.is_active(now=100.0) is True
    # Still active one ping interval later (within the TTL).
    assert scan_session.is_active(now=110.0) is True
    # Past the TTL with no fresh ping, the session lapses on its own.
    assert scan_session.is_active(now=100.0 + scan_session.SESSION_TTL + 1) is False


def test_state_reports_expiry_countdown():
    scan_session.ping(now=200.0)
    s = scan_session.state(now=205.0)
    assert s["active"] is True
    assert 0 < s["expires_in"] <= scan_session.SESSION_TTL


def _forget_in_memory():
    """Simulate a different worker (or a restart): only the file remains."""
    scan_session._state["last_ping"] = 0.0
    scan_session._state["mtime"] = None


def test_session_shared_across_workers(tmp_path):
    scan_session.ping(now=300.0)
    assert (tmp_path / "scan_session.json").exists()
    _forget_in_memory()
    # A worker that never saw the ping reads the shared heartbeat.
    assert scan_session.is_active(now=305.0) is True


def test_corrupt_state_file_never_breaks_a_read(tmp_path):
    scan_session.ping(now=300.0)
    _forget_in_memory()
    (tmp_path / "scan_session.json").write_text("{not json")
    # A torn/corrupt file falls back to the in-memory heartbeat (0) instead of
    # raising: the session simply reads as inactive.
    assert scan_session.is_active(now=305.0) is False


def test_unwritable_data_dir_degrades_to_in_memory(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", "/nonexistent/nowhere", raising=False)
    _forget_in_memory()
    # No file can be written or read, but the heartbeat still works in-process.
    scan_session.ping(now=400.0)
    assert scan_session.is_active(now=402.0) is True


# -- TestClient fixture -------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    # Configure Grocy so the setup-redirect middleware is a no-op.
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "vision_provider", "gemini", raising=False)
    monkeypatch.setattr(settings, "gemini_api_key", "k", raising=False)
    scanner_mode.reset()
    scan_session.reset()
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)
        scanner_mode.reset()
        scan_session.reset()


def _clear_barcode(barcode: str) -> None:
    """Drop any leftover pending row for this barcode so a scan takes the
    fresh-queue path rather than merging with a prior run's row (the DB file is
    shared across the session)."""
    from app.database import SessionLocal
    from app.models.db_models import PendingItem
    db = SessionLocal()
    try:
        db.query(PendingItem).filter(PendingItem.barcode == barcode).delete()
        db.commit()
    finally:
        db.close()


# -- Fast-ack + background enrichment -----------------------------------------

def test_inventory_scan_fast_acks_without_synchronous_lookup(client, monkeypatch):
    scanner_mode.set_mode("inventory")
    from app.routers import pending as pending_router
    _clear_barcode("0111000000017")

    spawned = []
    monkeypatch.setattr(pending_router, "_spawn_enrichment",
                        lambda item_id, barcode: spawned.append((item_id, barcode)))

    # If the barcode lookup ran inline the ack would not be fast: make it fail
    # loudly so a synchronous call cannot slip in unnoticed.
    async def _boom(barcode, db):
        raise AssertionError("lookup must run in the background, not inline")
    monkeypatch.setattr(pending_router, "lookup_barcode", _boom)

    r = client.post("/pending/scan", json={"barcode": "0111000000017"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued"
    assert body["enriching"] is True
    assert body["message"] == "Saved, looking up..."
    item = body["item"]
    assert item["enriching"] is True
    assert item["name"].startswith("Unknown")
    # The background lookup was scheduled with the new row id and the barcode.
    assert spawned == [(item["id"], "0111000000017")]


def test_background_enrich_fills_in_the_name(client, monkeypatch):
    scanner_mode.set_mode("inventory")
    from app.routers import pending as pending_router
    from app.models.food import FoodItem, FoodCategory
    _clear_barcode("0222000000024")

    monkeypatch.setattr(pending_router, "_spawn_enrichment", lambda *a: None)
    item_id = client.post(
        "/pending/scan", json={"barcode": "0222000000024"}).json()["item"]["id"]

    async def _lookup(barcode, db):
        return FoodItem(name="Dr Pepper Zero", category=FoodCategory.beverages)
    monkeypatch.setattr(pending_router, "lookup_barcode", _lookup)
    asyncio.run(pending_router.enrich_pending_item(item_id, "0222000000024"))

    row = next(r for r in client.get("/pending/").json()["items"]
               if r["id"] == item_id)
    assert row["name"] == "Dr Pepper Zero"
    assert row["enriching"] is False
    assert row["lookup_failed"] is False


def test_background_enrich_marks_lookup_failed_on_not_found(client, monkeypatch):
    scanner_mode.set_mode("inventory")
    from app.routers import pending as pending_router
    from app.services.barcode import BarcodeNotFound
    _clear_barcode("0333000000031")

    monkeypatch.setattr(pending_router, "_spawn_enrichment", lambda *a: None)
    item_id = client.post(
        "/pending/scan", json={"barcode": "0333000000031"}).json()["item"]["id"]

    async def _lookup(barcode, db):
        raise BarcodeNotFound(barcode)
    monkeypatch.setattr(pending_router, "lookup_barcode", _lookup)
    asyncio.run(pending_router.enrich_pending_item(item_id, "0333000000031"))

    row = next(r for r in client.get("/pending/").json()["items"]
               if r["id"] == item_id)
    assert row["enriching"] is False
    assert row["lookup_failed"] is True
    assert row["name"].startswith("Unknown")


def test_background_enrich_never_raises_on_crash(client, monkeypatch):
    """A lookup that blows up must not crash the app: the row is left flagged
    for the user to fix and the enriching flag is always cleared."""
    scanner_mode.set_mode("inventory")
    from app.routers import pending as pending_router
    _clear_barcode("0444000000048")

    monkeypatch.setattr(pending_router, "_spawn_enrichment", lambda *a: None)
    item_id = client.post(
        "/pending/scan", json={"barcode": "0444000000048"}).json()["item"]["id"]

    async def _lookup(barcode, db):
        raise RuntimeError("provider exploded")
    monkeypatch.setattr(pending_router, "lookup_barcode", _lookup)
    # Does not raise.
    asyncio.run(pending_router.enrich_pending_item(item_id, "0444000000048"))

    row = next(r for r in client.get("/pending/").json()["items"]
               if r["id"] == item_id)
    assert row["enriching"] is False
    assert row["lookup_failed"] is True


# -- /gadgets/config scanner_uart contract ------------------------------------

def test_gadgets_config_carries_scanner_uart(client):
    scanner_mode.set_mode("inventory")
    r = client.get("/gadgets/config")
    assert r.status_code == 200
    cfg = r.json()
    # The hardware block is nested; the session state is top-level (the shape
    # the host reader daemon consumes).
    su = cfg["scanner_uart"]
    assert su["enabled"] is False
    assert su["port"] == "/dev/serial0"
    assert su["baud"] == 9600
    assert cfg["scan_active"] is False
    assert cfg["scanner_mode"] == "inventory"


def test_gadgets_config_reflects_enabled_active_and_mode(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "scanner_uart_enabled", True, raising=False)
    monkeypatch.setattr(settings, "scanner_uart_port", "/dev/ttyAMA0", raising=False)
    monkeypatch.setattr(settings, "scanner_uart_baud", 115200, raising=False)
    scanner_mode.set_mode("shopping")
    # An open scan page (a heartbeat) turns scan_active on.
    client.post("/pending/scan-session/ping")

    cfg = client.get("/gadgets/config").json()
    su = cfg["scanner_uart"]
    assert su["enabled"] is True
    assert su["port"] == "/dev/ttyAMA0"
    assert su["baud"] == 115200
    assert cfg["scan_active"] is True
    assert cfg["scanner_mode"] == "shopping"


# -- Settings plumbing --------------------------------------------------------

def test_scanner_uart_settings_are_saveable(monkeypatch, tmp_path):
    from app.config import settings, _SAVEABLE
    assert "scanner_uart_enabled" in _SAVEABLE
    assert "scanner_uart_port" in _SAVEABLE
    assert "scanner_uart_baud" in _SAVEABLE

    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    settings.save({"scanner_uart_enabled": True,
                   "scanner_uart_port": "/dev/ttyAMA0",
                   "scanner_uart_baud": 115200})
    assert settings.scanner_uart_enabled is True
    assert settings.scanner_uart_port == "/dev/ttyAMA0"
    assert settings.scanner_uart_baud == 115200


# -- Scan-session endpoints and the Manage page -------------------------------

def test_scan_session_ping_and_state(client):
    assert client.get("/pending/scan-session").json()["active"] is False
    ping = client.post("/pending/scan-session/ping")
    assert ping.status_code == 200
    assert ping.json()["active"] is True
    assert client.get("/pending/scan-session").json()["active"] is True


def test_manage_page_renders_scan_session_hooks(client):
    r = client.get("/ui/add")
    assert r.status_code == 200
    html = r.text
    # The live banner, the heartbeat call, and the live scan list are all
    # wired. The heartbeat lives in the page's static script (the page logic
    # moved out of the template for kiosk caching, FoodAssistant-3c7k).
    assert "scanner-live-banner" in html
    assert "scan-live-list" in html
    page_js = (SERVICE / "app" / "static" / "js" / "manage-pantry.js").read_text()
    assert "pending/scan-session/ping" in page_js
