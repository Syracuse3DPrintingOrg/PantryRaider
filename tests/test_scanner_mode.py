"""Tests for the barcode scanner mode (FoodAssistant-8jbk).

Covers the mode store (cycle/set/reset), its state-file persistence across
workers and restarts (FoodAssistant-3jxk), and the scan endpoint dispatch:
the default "inventory" mode is unchanged, while "consume" and "shopping"
route the barcode to Grocy/Mealie and never hard-fail.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import scanner_mode  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_mode(monkeypatch, tmp_path):
    # Point the state file at a per-test dir so persistence is exercised
    # without touching a real data_dir.
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    scanner_mode.reset()
    yield
    scanner_mode.reset()


def test_mode_defaults_to_inventory():
    assert scanner_mode.get_mode() == "inventory"
    assert scanner_mode.get_state()["label"] == "Stock"


def test_cycle_wraps_through_all_modes():
    seen = [scanner_mode.get_mode()]
    for _ in range(len(scanner_mode.SCANNER_MODES)):
        seen.append(scanner_mode.cycle_mode()["mode"])
    # Cycled through every mode and wrapped back to the start.
    assert seen[0] == "inventory"
    assert set(seen) == set(scanner_mode.SCANNER_MODES)
    assert seen[-1] == "inventory"


def test_set_unknown_mode_falls_back():
    assert scanner_mode.set_mode("nonsense")["mode"] == "inventory"
    assert scanner_mode.set_mode("consume")["mode"] == "consume"


# State-file persistence (FoodAssistant-3jxk) --------------------------------

def _forget_in_memory_state():
    """Simulate a different worker process (or a restart): the module-level
    state is back at its import-time default, only the file remains."""
    scanner_mode._state["mode"] = "inventory"
    scanner_mode._state["mtime"] = None


def test_mode_is_shared_across_workers(tmp_path):
    scanner_mode.set_mode("shopping")
    assert (tmp_path / "scanner_mode.json").exists()
    _forget_in_memory_state()
    # A worker that never saw the set_mode still reads the shared mode.
    assert scanner_mode.get_mode() == "shopping"


def test_mode_survives_restart(tmp_path):
    scanner_mode.cycle_mode()   # inventory -> consume
    _forget_in_memory_state()
    assert scanner_mode.get_state() == {"mode": "consume", "label": "Use"}


def test_cycle_reads_shared_state_first(tmp_path):
    # Another worker set "shopping"; this worker's cycle must advance from
    # there (to "audit"), not from its own stale in-memory "inventory".
    (tmp_path / "scanner_mode.json").write_text(json.dumps({"mode": "shopping"}))
    assert scanner_mode.cycle_mode()["mode"] == "audit"


def test_corrupt_state_file_never_breaks_a_read(tmp_path):
    scanner_mode.set_mode("consume")
    _forget_in_memory_state()
    (tmp_path / "scanner_mode.json").write_text("{not json")
    # A torn/corrupt file falls back to the in-memory mode instead of raising.
    assert scanner_mode.get_mode() == "inventory"


def test_unwritable_data_dir_degrades_to_in_memory(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", "/nonexistent/nowhere", raising=False)
    scanner_mode._state["mode"] = "inventory"
    scanner_mode._state["mtime"] = None
    # No file can be written or read, but the mode still works process-locally.
    assert scanner_mode.set_mode("audit")["mode"] == "audit"
    assert scanner_mode.get_mode() == "audit"


# Scan dispatch -------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    # Make is_configured() true so the setup-redirect middleware is a no-op.
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "vision_provider", "gemini", raising=False)
    monkeypatch.setattr(settings, "gemini_api_key", "k", raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_consume_mode_calls_grocy(client, monkeypatch):
    scanner_mode.set_mode("consume")
    called = {}

    async def _consume(self, barcode, amount=1.0):
        called["barcode"] = barcode
        called["amount"] = amount
        return {"ok": True}

    from app.services.grocy import GrocyClient
    monkeypatch.setattr(GrocyClient, "consume_by_barcode", _consume)
    r = client.post("/pending/scan", json={"barcode": "12345", "quantity": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "consumed"
    assert called == {"barcode": "12345", "amount": 2}


def test_consume_failure_returns_status_not_500(client, monkeypatch):
    scanner_mode.set_mode("consume")

    async def _boom(self, barcode, amount=1.0):
        raise RuntimeError("unknown barcode")

    from app.services.grocy import GrocyClient
    monkeypatch.setattr(GrocyClient, "consume_by_barcode", _boom)
    r = client.post("/pending/scan", json={"barcode": "999"})
    assert r.status_code == 200
    assert r.json()["status"] == "consume_failed"


def test_overlong_barcode_is_rejected_not_queued(client):
    """A concatenated barcode (buffer that never cleared) is refused instead of
    creating a nonsense pending item (FoodAssistant-doz6)."""
    scanner_mode.set_mode("inventory")
    junk = "1" * 60
    r = client.post("/pending/scan", json={"barcode": junk})
    assert r.status_code == 200
    body = r.json()
    # Refused before any lookup/queue, so the garbage never becomes a pending row.
    assert body["status"] == "rejected"
    assert body["length"] == 60


def test_plausible_long_barcode_still_accepted(client, monkeypatch):
    """A GS1 variable-weight code (up to ~22 digits) is below the cap and still
    queues, so the guard does not reject legitimate longer barcodes."""
    scanner_mode.set_mode("inventory")
    from app.routers import pending as pending_router

    async def _lookup(barcode, db):
        from app.models.food import FoodItem
        return FoodItem(name="Ground Beef")

    monkeypatch.setattr(pending_router, "lookup_barcode", _lookup)
    r = client.post("/pending/scan", json={"barcode": "021248141011152083353"})
    assert r.status_code == 200
    assert r.json().get("status") != "rejected"


def test_gtin_check_digit_validation():
    from app.routers.pending import gtin_check_digit_ok
    # Real UPC-A (Dr Pepper Cherry Zero Sugar) validates.
    assert gtin_check_digit_ok("078000035483") is True
    # A valid EAN-13 validates.
    assert gtin_check_digit_ok("4006381333931") is True
    # A single corrupted digit fails the check.
    assert gtin_check_digit_ok("078000035484") is False
    # Non-GTIN lengths and non-digit codes are NOT rejected (cannot validate).
    assert gtin_check_digit_ok("035483") is True        # 6 digits
    assert gtin_check_digit_ok("0780003583") is True     # 10 digits
    assert gtin_check_digit_ok("ABC123") is True


def test_misread_barcode_still_queues_not_silently_rejected(client, monkeypatch):
    """A misread (bad check digit) must NOT be silently dropped: the headless
    scanner UI cannot show a rejection, so dropping it makes scanning look
    broken. It queues (as Unknown) for the user to fix (FoodAssistant-pmry)."""
    scanner_mode.set_mode("inventory")
    from app.routers import pending as pending_router

    async def _lookup(barcode, db):
        from app.services.barcode import BarcodeNotFound
        raise BarcodeNotFound(barcode)

    monkeypatch.setattr(pending_router, "lookup_barcode", _lookup)
    r = client.post("/pending/scan", json={"barcode": "078000035484"})  # bad check digit
    assert r.status_code == 200
    assert r.json().get("status") != "rejected"


def test_valid_upc_is_accepted(client, monkeypatch):
    scanner_mode.set_mode("inventory")
    from app.routers import pending as pending_router

    async def _lookup(barcode, db):
        from app.models.food import FoodItem
        return FoodItem(name="Dr Pepper")

    monkeypatch.setattr(pending_router, "lookup_barcode", _lookup)
    r = client.post("/pending/scan", json={"barcode": "078000035483"})
    assert r.status_code == 200
    assert r.json().get("status") != "rejected"


def test_scanner_mode_endpoints(client):
    assert client.get("/pending/scanner-mode").json()["mode"] == "inventory"
    cycled = client.post("/pending/scanner-mode/cycle").json()
    assert cycled["mode"] == "consume"
    set_back = client.post("/pending/scanner-mode", json={"mode": "shopping"}).json()
    assert set_back["mode"] == "shopping"


def test_cycle_changes_scan_routing_end_to_end(client, monkeypatch):
    """The whole FoodAssistant-ewyo chain on a server: the cycle endpoint the
    Stream Deck key posts must change how the very next scan routes, because
    both read the same shared mode state."""
    called = {}

    async def _consume(self, barcode, amount=1.0):
        called["barcode"] = barcode
        return {"ok": True}

    from app.services.grocy import GrocyClient
    monkeypatch.setattr(GrocyClient, "consume_by_barcode", _consume)
    assert client.post("/pending/scanner-mode/cycle").json()["mode"] == "consume"
    r = client.post("/pending/scan", json={"barcode": "078000035483"})
    assert r.json()["status"] == "consumed"
    assert called["barcode"] == "078000035483"


# Satellite forwarding (FoodAssistant-ewyo) -----------------------------------
#
# On a pi_remote every scanner-mode call and every scan must forward to the
# main server, the single owner of the mode. If any one of them read or wrote
# the satellite's local state instead, a deck cycle taken on the satellite and
# a scan handled by the server would disagree about the active mode.

class _FwdRecorder:
    """Stands in for pending._fwd_client: records every forwarded request and
    answers like a main server that just cycled to consume."""

    def __init__(self):
        self.calls: list[dict] = []

    async def request(self, method, url, headers=None, params=None, content=None):
        import httpx
        self.calls.append({
            "method": method, "url": url,
            "api_key": (headers or {}).get("X-API-Key", ""),
        })
        return httpx.Response(200, json={"mode": "consume", "label": "Use",
                                         "from": "main-server"})


@pytest.fixture
def sat_client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "remote_server_url", "http://main.server:9284", raising=False)
    monkeypatch.setattr(settings, "upstream_api_key", "sat-key", raising=False)
    from app.routers import pending as pending_router
    recorder = _FwdRecorder()
    monkeypatch.setattr(pending_router, "_fwd_client", recorder)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app), recorder, tmp_path
    finally:
        os.chdir(cwd)


def test_satellite_forwards_every_scanner_mode_call(sat_client):
    client, recorder, tmp_path = sat_client
    responses = [
        client.get("/pending/scanner-mode"),
        client.post("/pending/scanner-mode", json={"mode": "consume"}),
        client.post("/pending/scanner-mode/cycle"),
        client.post("/pending/scan", json={"barcode": "078000035483"}),
    ]
    assert [c["url"] for c in recorder.calls] == [
        "http://main.server:9284/pending/scanner-mode",
        "http://main.server:9284/pending/scanner-mode",
        "http://main.server:9284/pending/scanner-mode/cycle",
        "http://main.server:9284/pending/scan",
    ]
    # Every call authenticates with the satellite's upstream key and returns
    # the server's answer verbatim (the deck face shows the server's label).
    assert all(c["api_key"] == "sat-key" for c in recorder.calls)
    for r in responses:
        assert r.status_code == 200
        assert r.json()["from"] == "main-server"


def test_satellite_never_touches_local_mode_state(sat_client):
    client, recorder, tmp_path = sat_client
    client.post("/pending/scanner-mode", json={"mode": "consume"})
    client.post("/pending/scanner-mode/cycle")
    client.post("/pending/scan", json={"barcode": "078000035483"})
    # The single source of truth is the main server: no local state file may
    # appear on the satellite, and the in-process mode stays at its default.
    assert not (tmp_path / "scanner_mode.json").exists()
    assert scanner_mode._state["mode"] == "inventory"
