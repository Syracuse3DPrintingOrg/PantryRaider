"""Tests for pantry audit mode (FoodAssistant-ugku).

Covers the pure session logic (start/record/status/stop, matching,
missing/unexpected), its state-file persistence across workers and restarts
(FoodAssistant-60hl), and the /audit endpoints via TestClient with Grocy stock
mocked, plus the scanner-mode "audit" dispatch in /pending/scan. Counting is
read only: no scan or status call ever writes to Grocy. The one opt-in write
is POST /audit/apply (FoodAssistant-d5s0), covered here with a truth table for
the pure corrections builder (including the unseen vs counted-zero
distinction), a faked GrocyClient for the endpoint, and the satellite forward.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import audit  # noqa: E402
from app.services import scanner_mode  # noqa: E402


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    # Point the state files at a per-test dir so persistence is exercised
    # without touching a real data_dir.
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
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


def test_is_all_areas_recognizes_the_whole_pantry_scope():
    assert audit.is_all_areas("")
    assert audit.is_all_areas("__all__")
    assert audit.is_all_areas("All areas")
    assert audit.is_all_areas("all")
    assert not audit.is_all_areas("Fridge")


def test_start_all_areas_stores_the_friendly_label():
    # A sentinel location is normalized to the display label, so every surface
    # shows "All areas" rather than "__all__" or an empty value.
    audit.start("__all__", [{"name": "Milk"}, {"name": "Rice"}])
    assert audit.get_location() == "All areas"
    assert audit.status()["counts"]["expected"] == 2


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


# Apply corrections: the pure builder (FoodAssistant-d5s0) --------------------
#
# build_corrections turns the status() "expected" list into the inventory
# corrections an Apply run would make. The truth table below pins the counted
# vs unseen semantics: "counted" means seen (scanned at least once in the
# current session shape), and an item never scanned is never written, and
# never zeroed, because unseen can mean "not scanned yet" as well as "gone".


def _exp(name="Milk", *, seen, scanned_count, amount, product_id=1):
    return {"name": name, "amount": amount, "product_id": product_id,
            "seen": seen, "scanned_count": scanned_count}


def test_build_corrections_counted_under_and_over():
    out = audit.build_corrections([
        _exp("Milk", seen=True, scanned_count=1, amount=2, product_id=1),
        _exp("Eggs", seen=True, scanned_count=3, amount=1, product_id=2),
    ])
    assert out == [
        {"product_id": 1, "name": "Milk", "counted": 1.0, "expected": 2.0},
        {"product_id": 2, "name": "Eggs", "counted": 3.0, "expected": 1.0},
    ]


def test_build_corrections_matching_count_is_not_corrected():
    out = audit.build_corrections([
        _exp(seen=True, scanned_count=2, amount=2),
        _exp("Rice", seen=True, scanned_count=1, amount=1.0, product_id=3),
    ])
    assert out == []


def test_build_corrections_never_zeroes_an_unseen_item():
    # The heart of the read-only promise: an expected item that was simply
    # never scanned is not a counted zero and must never be written.
    out = audit.build_corrections([
        _exp(seen=False, scanned_count=0, amount=2),
    ])
    assert out == []


def test_build_corrections_a_true_counted_zero_would_zero():
    # The other side of the distinction: an entry explicitly marked seen with
    # a zero count IS a counted zero and corrects the stock to zero. The
    # current session shape cannot produce this (a scan always records at
    # least one), but the builder's semantics are pinned here so a future
    # "mark as zero" affordance inherits the right behavior.
    out = audit.build_corrections([
        _exp(seen=True, scanned_count=0, amount=2),
    ])
    assert out == [{"product_id": 1, "name": "Milk", "counted": 0.0, "expected": 2.0}]


def test_build_corrections_skips_unknown_amount_and_missing_product_id():
    out = audit.build_corrections([
        # No expected amount: no discrepancy can be established.
        _exp(seen=True, scanned_count=1, amount=None),
        # No product id (a session started before the field existed): nothing
        # in Grocy to address.
        _exp("Eggs", seen=True, scanned_count=1, amount=2, product_id=None),
    ])
    assert out == []


def test_build_corrections_empty_and_missing_input():
    assert audit.build_corrections([]) == []
    assert audit.build_corrections(None) == []


def test_start_keeps_product_id_and_status_exposes_it():
    audit.start("Fridge", [{"name": "Milk", "amount": 2, "product_id": 7}])
    e = audit.status()["expected"][0]
    assert e["product_id"] == 7


def test_record_corrections_folds_applied_amounts_into_the_snapshot():
    audit.start("Fridge", [{"name": "Milk", "amount": 2, "product_id": 1},
                           {"name": "Eggs", "amount": 12, "product_id": 2}])
    audit.record_scan("Milk")
    audit.record_corrections([{"name": "Milk", "amount": 1.0}])
    s = audit.status()
    by_name = {e["name"]: e for e in s["expected"]}
    assert by_name["Milk"]["amount"] == 1.0     # updated to what was written
    assert by_name["Eggs"]["amount"] == 12      # untouched
    # The discrepancy is gone, so a re-run has nothing to apply.
    assert audit.build_corrections(s["expected"]) == []


def test_record_corrections_without_a_session_is_a_noop():
    audit.record_corrections([{"name": "Milk", "amount": 1.0}])
    assert audit.is_active() is False


# State-file persistence (FoodAssistant-60hl) --------------------------------

def _forget_in_memory_state():
    """Simulate a different worker process (or a restart): the module-level
    state is back at its import-time default, only the file remains."""
    audit._state.clear()
    audit._state["active"] = False
    audit._mtime = None


def test_session_is_shared_across_workers(tmp_path):
    audit.start("Fridge", [{"name": "Milk", "amount": 2}])
    audit.record_scan("Milk")
    assert (tmp_path / "audit_session.json").exists()
    _forget_in_memory_state()
    # A worker that never saw the start still sees the same session.
    assert audit.is_active() is True
    assert audit.get_location() == "Fridge"
    s = audit.status()
    assert s["counts"] == {"expected": 1, "seen": 1, "missing": 0, "unexpected": 0}


def test_scan_recorded_by_another_worker_lands_in_the_session(tmp_path):
    # Worker A starts the audit; worker B (fresh in-memory state) records a
    # scan; worker A's next status must include it.
    audit.start("Fridge", [{"name": "Milk"}, {"name": "Eggs"}])
    _forget_in_memory_state()
    assert audit.record_scan("Eggs")["status"] == "matched"
    _forget_in_memory_state()
    assert audit.status()["missing"] == ["Milk"]


def test_session_survives_module_reimport(tmp_path):
    audit.start("Pantry", [{"name": "Rice"}])
    audit.record_scan("Rice")
    # A fresh module load (a restarted app) re-reads the persisted session.
    importlib.reload(audit)
    assert audit.is_active() is True
    assert audit.get_location() == "Pantry"
    assert audit.status()["counts"]["seen"] == 1


def test_stop_clears_the_session_for_every_worker(tmp_path):
    audit.start("Fridge", [{"name": "Milk"}])
    audit.stop()
    _forget_in_memory_state()
    assert audit.is_active() is False


def test_corrupt_state_file_degrades_safely(tmp_path):
    audit.start("Fridge", [{"name": "Milk"}])
    (tmp_path / "audit_session.json").write_text("{not json")
    # A fresh worker facing only the corrupt file starts inactive, not dead.
    _forget_in_memory_state()
    assert audit.is_active() is False
    assert audit.status()["active"] is False


def test_corrupt_state_file_never_breaks_the_active_worker(tmp_path):
    audit.start("Fridge", [{"name": "Milk"}])
    (tmp_path / "audit_session.json").write_text("{not json")
    # A torn/corrupt file never breaks a call: the in-memory session carries
    # on, and the next successful write repairs the file for other workers.
    assert audit.is_active() is True
    assert audit.record_scan("Milk")["status"] == "matched"
    _forget_in_memory_state()
    assert audit.status()["counts"]["seen"] == 1


def test_wrong_shape_state_file_is_ignored(tmp_path):
    (tmp_path / "audit_session.json").write_text(json.dumps(["not", "a", "session"]))
    assert audit.is_active() is False


def test_unwritable_data_dir_degrades_to_in_memory(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", "/nonexistent/nowhere", raising=False)
    audit._state.clear()
    audit._state["active"] = False
    audit._mtime = None
    # No file can be written or read, but the session still works process-locally.
    audit.start("Fridge", [{"name": "Milk"}])
    assert audit.record_scan("Milk")["status"] == "matched"
    assert audit.status()["counts"]["seen"] == 1


# Endpoints -----------------------------------------------------------------

_STOCK = [
    {"product_id": 1, "name": "Milk", "amount": 2, "days_remaining": 5, "location_name": "Fridge", "storage_bucket": "fridge"},
    {"product_id": 2, "name": "Eggs", "amount": 12, "days_remaining": 10, "location_name": "Fridge", "storage_bucket": "fridge"},
    {"product_id": 3, "name": "Rice", "amount": 1, "days_remaining": 300, "location_name": "Pantry", "storage_bucket": "pantry"},
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


def test_pending_scan_audit_mode_auto_starts_all_areas(client, monkeypatch):
    """In audit mode with no session, a scan from Manage auto-starts a
    whole-pantry count (the full stock) and records the scan, instead of
    refusing it. This is what makes the Audit key work from the Manage page."""
    scanner_mode.set_mode("audit")
    from app.routers import pending as pending_router

    async def _lookup(barcode, db):
        from app.models.food import FoodItem
        return FoodItem(name="Milk")

    monkeypatch.setattr(pending_router, "lookup_barcode", _lookup)
    r = client.post("/pending/scan", json={"barcode": "333"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "audit"
    assert body["status"] == "matched"        # Milk is in the full stock
    s = client.get("/audit/status").json()
    assert s["active"] is True
    assert s["location"] == "All areas"
    assert s["counts"]["expected"] == 3       # every area, not one location
    assert s["counts"]["seen"] == 1


def test_start_all_areas_counts_full_stock(client):
    """No location (or the sentinel) audits the whole pantry: expected stock is
    every area, stored under the "All areas" label."""
    started = client.post("/audit/start", json={"location": "__all__"}).json()
    assert started["location"] == "All areas"
    assert started["counts"]["expected"] == 3
    client.post("/audit/stop")
    # An empty body means all areas too (the picker's default).
    started2 = client.post("/audit/start", json={}).json()
    assert started2["location"] == "All areas"
    assert started2["counts"]["expected"] == 3


# POST /audit/apply (FoodAssistant-d5s0) --------------------------------------


def _fake_inventory(monkeypatch, fail_ids=()):
    """Fake GrocyClient.set_stock_amount: records calls, fails for fail_ids."""
    from app.services.grocy import GrocyClient, GrocyError
    calls: list[tuple] = []

    async def _set(self, product_id, amount, best_before_date=None):
        if product_id in fail_ids:
            raise GrocyError("Grocy said no")
        calls.append((product_id, amount))
        return {"ok": True}

    monkeypatch.setattr(GrocyClient, "set_stock_amount", _set)
    return calls


def test_apply_corrects_only_counted_items(client, monkeypatch):
    """Milk was counted (1 seen vs 2 expected) so it is corrected; Eggs was
    never scanned so it is never touched, and never zeroed."""
    calls = _fake_inventory(monkeypatch)
    client.post("/audit/start", json={"location": "Fridge"})
    client.post("/audit/scan", json={"name": "Milk"})
    r = client.post("/audit/apply")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"] == "1 corrected"
    assert body["failed"] == []
    assert body["applied"] == [{"name": "Milk", "amount": 1.0, "was": 2.0}]
    assert calls == [(1, 1.0)]  # only Milk; Eggs (pid 2) was never written


def test_apply_folds_result_back_so_a_rerun_is_a_noop(client, monkeypatch):
    calls = _fake_inventory(monkeypatch)
    client.post("/audit/start", json={"location": "Fridge"})
    client.post("/audit/scan", json={"name": "Milk"})
    client.post("/audit/apply")
    # The session snapshot now matches Grocy, so the page stops offering the
    # correction and a second Apply writes nothing.
    s = client.get("/audit/status").json()
    milk = [e for e in s["expected"] if e["name"] == "Milk"][0]
    assert milk["amount"] == 1.0
    r2 = client.post("/audit/apply")
    assert r2.json()["summary"] == "Nothing to correct."
    assert calls == [(1, 1.0)]


def test_apply_partial_failure_is_reported_not_fatal(client, monkeypatch):
    """One product failing does not stop the rest: the others still apply and
    the response says which succeeded and which failed."""
    calls = _fake_inventory(monkeypatch, fail_ids={2})
    client.post("/audit/start", json={"location": "Fridge"})
    client.post("/audit/scan", json={"name": "Milk"})   # 1 vs 2: correct to 1
    client.post("/audit/scan", json={"name": "Eggs"})   # 1 vs 12: fails
    r = client.post("/audit/apply")
    body = r.json()
    assert body["summary"] == "1 corrected, 1 failed"
    assert [a["name"] for a in body["applied"]] == ["Milk"]
    assert [f["name"] for f in body["failed"]] == ["Eggs"]
    assert "Grocy said no" in body["failed"][0]["error"]
    assert calls == [(1, 1.0)]
    # Only the applied item folds back; the failed one stays discrepant so a
    # retry offers exactly it again.
    s = client.get("/audit/status").json()
    by_name = {e["name"]: e for e in s["expected"]}
    assert by_name["Milk"]["amount"] == 1.0
    assert by_name["Eggs"]["amount"] == 12


def test_apply_without_a_session_is_a_400(client, monkeypatch):
    calls = _fake_inventory(monkeypatch)
    r = client.post("/audit/apply")
    assert r.status_code == 400
    assert calls == []


def test_apply_with_nothing_counted_writes_nothing(client, monkeypatch):
    """A session where every scan matches (and the rest is merely unseen) has
    nothing to correct: no write happens at all."""
    calls = _fake_inventory(monkeypatch)
    client.post("/audit/start", json={"location": "Fridge"})
    client.post("/audit/scan", json={"name": "Milk"})
    client.post("/audit/scan", json={"name": "Milk"})   # count 2 == expected 2
    r = client.post("/audit/apply")
    assert r.json() == {"applied": [], "failed": [], "summary": "Nothing to correct."}
    assert calls == []


# Satellite forwarding: /audit/apply must reach the main server ---------------


class _FwdRecorder:
    """Stands in for audit._fwd_client: records every forwarded request and
    answers like a main server that just applied two corrections."""

    def __init__(self):
        self.calls: list[dict] = []

    async def request(self, method, url, headers=None, params=None, content=None):
        import httpx
        self.calls.append({
            "method": method, "url": url,
            "api_key": (headers or {}).get("X-API-Key", ""),
        })
        return httpx.Response(200, json={"applied": [], "failed": [],
                                         "summary": "2 corrected",
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
    from app.routers import audit as audit_router
    recorder = _FwdRecorder()
    monkeypatch.setattr(audit_router, "_fwd_client", recorder)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app), recorder
    finally:
        os.chdir(cwd)


def test_satellite_forwards_apply_to_the_main_server(sat_client):
    """On a pi_remote, Apply corrections must land on the main server (the
    inventory owner), exactly like every other /audit call: same URL shape,
    same upstream key, the server's answer returned verbatim."""
    client, recorder = sat_client
    r = client.post("/audit/apply")
    assert [c["url"] for c in recorder.calls] == ["http://main.server:9284/audit/apply"]
    assert recorder.calls[0]["method"] == "POST"
    assert recorder.calls[0]["api_key"] == "sat-key"
    assert r.status_code == 200
    assert r.json()["summary"] == "2 corrected"
    assert r.json()["from"] == "main-server"
