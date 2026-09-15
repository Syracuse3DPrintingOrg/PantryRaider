"""Grocycode on food labels (FoodAssistant-28f3).

Three seams, none needing a printer, Grocy, or the network:
  * the pure pieces: parse_grocycode, the newest-entry picker, the consume
    booking reader, and the reply message builder;
  * the scan route: a grocycode payload consumes the exact stock entry in any
    scanner mode, stays read-only in audit mode, and never hard-fails;
  * the printing router: label endpoints resolve and attach the grocycode the
    renderer draws.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services.grocy import GrocyClient, GrocyError, parse_grocycode  # noqa: E402
from app.routers import pending as pending_router  # noqa: E402
from app.routers import printing as printing_router  # noqa: E402
from app.services import scanner_mode  # noqa: E402


# -- parse_grocycode ---------------------------------------------------------


def test_parse_grocycode_entry_form():
    assert parse_grocycode("grcy:p:12:6a28c889c1193") == (12, "6a28c889c1193")
    # Surrounding whitespace from a scanner buffer is tolerated.
    assert parse_grocycode("  grcy:p:7:abc123  ") == (7, "abc123")


def test_parse_grocycode_product_only_form():
    assert parse_grocycode("grcy:p:12") == (12, "")


def test_parse_grocycode_rejects_junk():
    for junk in (
        "",                       # empty
        "grcy",                   # no segments
        "grcy:p",                 # no id
        "grcy:p:",                # empty id
        "grcy:p:abc",             # non-numeric id
        "grcy:p:0",               # zero id
        "grcy:p:-4",              # negative id
        "grcy:c:3",               # a chore code, not a product
        "grcy:b:1",               # a battery code
        "grcy:p:12:6a28:extra",   # too many segments
        "grcy:p:12:",             # empty entry id
        "grcy:p:12:6a28 c889",    # whitespace inside the entry id
        "grcy:p:12:6a28-c889",    # punctuation inside the entry id
        "078000035483",           # a plain barcode
        "http://example.com",     # a URL
    ):
        assert parse_grocycode(junk) is None, junk


def test_parse_grocycode_never_raises_on_non_strings():
    assert parse_grocycode(None) is None


# -- GrocyClient.consume_stock_entry ----------------------------------------


def test_consume_stock_entry_posts_amount_and_entry_id(monkeypatch):
    posts = []

    async def fake_post(self, path, payload):
        posts.append((path, payload))
        return [{"best_before_date": "2026-07-22"}]

    monkeypatch.setattr(GrocyClient, "_post", fake_post)
    result = asyncio.run(GrocyClient().consume_stock_entry(12, "6a28c889c1193", 2.0))
    assert posts == [("/stock/products/12/consume",
                      {"amount": 2.0, "stock_entry_id": "6a28c889c1193"})]
    assert result == [{"best_before_date": "2026-07-22"}]


# -- Pure reply helpers ------------------------------------------------------


def test_booking_best_by_reads_grocy_booking_rows():
    rows = [{"id": 9, "amount": -1, "best_before_date": "2026-07-22"}]
    assert pending_router.booking_best_by(rows) == "2026-07-22"
    # A timestamped date is trimmed to its date part.
    assert pending_router.booking_best_by(
        [{"best_before_date": "2026-07-22 00:00:00"}]) == "2026-07-22"
    # Dict shape tolerated; malformed shapes degrade to "".
    assert pending_router.booking_best_by({"best_before_date": "2026-07-22"}) == "2026-07-22"
    assert pending_router.booking_best_by([]) == ""
    assert pending_router.booking_best_by(None) == ""
    assert pending_router.booking_best_by(["junk", 4]) == ""
    assert pending_router.booking_best_by([{"amount": -1}]) == ""


def test_grocycode_reply_message_reads_naturally():
    assert (pending_router.grocycode_reply_message("Chicken Stock", "2026-07-22")
            == "Used up Chicken Stock, best by Jul 22, 2026.")
    assert pending_router.grocycode_reply_message("Chicken Stock") == "Used up Chicken Stock."
    # No name resolved: still a sentence, never a bare code.
    assert pending_router.grocycode_reply_message("", "") == "Used up this item."


# -- newest_stock_entry_id (printing router) ---------------------------------


def test_newest_stock_entry_id_picks_latest_created():
    entries = [
        {"id": 1, "stock_id": "aaa", "amount": 1,
         "row_created_timestamp": "2026-07-01 08:00:00"},
        {"id": 3, "stock_id": "ccc", "amount": 1,
         "row_created_timestamp": "2026-07-20 09:30:00"},
        {"id": 2, "stock_id": "bbb", "amount": 1,
         "row_created_timestamp": "2026-07-10 12:00:00"},
    ]
    assert printing_router.newest_stock_entry_id(entries) == "ccc"


def test_newest_stock_entry_id_skips_empty_and_malformed_rows():
    entries = [
        "junk",
        {"id": 4, "amount": 1, "row_created_timestamp": "2026-07-21"},  # no stock_id
        {"id": 5, "stock_id": "ddd", "amount": 0,
         "row_created_timestamp": "2026-07-22"},                        # used up
        {"id": 6, "stock_id": "eee", "amount": "many",
         "row_created_timestamp": "2026-07-23"},                        # bad amount
        {"id": 7, "stock_id": "fff", "amount": 2,
         "row_created_timestamp": "2026-07-05"},
    ]
    assert printing_router.newest_stock_entry_id(entries) == "fff"
    assert printing_router.newest_stock_entry_id([]) == ""
    assert printing_router.newest_stock_entry_id(None) == ""


def test_newest_stock_entry_id_ties_break_on_row_id():
    entries = [
        {"id": 8, "stock_id": "ggg", "amount": 1,
         "row_created_timestamp": "2026-07-20 09:30:00"},
        {"id": 9, "stock_id": "hhh", "amount": 1,
         "row_created_timestamp": "2026-07-20 09:30:00"},
    ]
    assert printing_router.newest_stock_entry_id(entries) == "hhh"


# -- _resolve_grocycode (printing router) ------------------------------------


def test_resolve_grocycode_ready_string_wins():
    body = printing_router.LabelIn(product_id=5, stock_entry_id="abc",
                                   grocycode="grcy:p:9:zzz")
    assert asyncio.run(printing_router._resolve_grocycode(body)) == "grcy:p:9:zzz"


def test_resolve_grocycode_explicit_entry_pair():
    body = printing_router.LabelIn(product_id=5, stock_entry_id="6a28c889c1193")
    assert (asyncio.run(printing_router._resolve_grocycode(body))
            == "grcy:p:5:6a28c889c1193")


def test_resolve_grocycode_product_alone_uses_newest_entry(monkeypatch):
    async def fake_get(self, path):
        assert path == "/stock/products/5/entries"
        return [{"id": 1, "stock_id": "newest1", "amount": 1,
                 "row_created_timestamp": "2026-07-20"}]

    monkeypatch.setattr(GrocyClient, "_get", fake_get)
    body = printing_router.LabelIn(product_id=5)
    assert asyncio.run(printing_router._resolve_grocycode(body)) == "grcy:p:5:newest1"


def test_resolve_grocycode_degrades_to_plain_label(monkeypatch):
    # No product behind the label: no code.
    assert asyncio.run(printing_router._resolve_grocycode(
        printing_router.LabelIn(name="Free typed"))) == ""

    # Grocy unreachable: the label still prints, just without the code.
    async def boom(self, path):
        raise GrocyError("Grocy is not reachable. Inventory will return when it is.")

    monkeypatch.setattr(GrocyClient, "_get", boom)
    assert asyncio.run(printing_router._resolve_grocycode(
        printing_router.LabelIn(product_id=5))) == ""

    # A product with no usable entries: same plain-label fallback.
    async def empty(self, path):
        return []

    monkeypatch.setattr(GrocyClient, "_get", empty)
    assert asyncio.run(printing_router._resolve_grocycode(
        printing_router.LabelIn(product_id=5))) == ""


# -- Scan routing through the app -------------------------------------------


@pytest.fixture(autouse=True)
def _reset_mode(monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    scanner_mode.reset()
    yield
    scanner_mode.reset()


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
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _stub_products(monkeypatch, products):
    async def fake_products(self):
        return products

    monkeypatch.setattr(GrocyClient, "get_products", fake_products)


def test_grocycode_scan_consumes_exact_entry_in_inventory_mode(client, monkeypatch):
    scanner_mode.set_mode("inventory")
    _stub_products(monkeypatch, [{"id": 12, "name": "Chicken Stock"}])
    called = {}

    async def fake_consume_entry(self, product_id, stock_entry_id, amount=1.0):
        called.update(product_id=product_id, stock_entry_id=stock_entry_id,
                      amount=amount)
        return [{"best_before_date": "2026-07-22"}]

    monkeypatch.setattr(GrocyClient, "consume_stock_entry", fake_consume_entry)
    r = client.post("/pending/scan",
                    json={"barcode": "grcy:p:12:6a28c889c1193", "quantity": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "consumed"
    assert body["name"] == "Chicken Stock"
    assert body["best_by"] == "2026-07-22"
    assert body["message"] == "Used up Chicken Stock, best by Jul 22, 2026."
    assert called == {"product_id": 12, "stock_entry_id": "6a28c889c1193",
                      "amount": 1.0}


def test_grocycode_scan_consumes_in_shopping_mode_too(client, monkeypatch):
    # The label means "this container is being used up" in any mode.
    scanner_mode.set_mode("shopping")
    _stub_products(monkeypatch, [{"id": 12, "name": "Chicken Stock"}])

    async def fake_consume_entry(self, product_id, stock_entry_id, amount=1.0):
        return [{"best_before_date": "2026-07-22"}]

    monkeypatch.setattr(GrocyClient, "consume_stock_entry", fake_consume_entry)
    r = client.post("/pending/scan", json={"barcode": "grcy:p:12:6a28c889c1193"})
    assert r.status_code == 200
    assert r.json()["status"] == "consumed"
    assert r.json()["mode"] == "shopping"


def test_grocycode_scan_longer_than_barcode_cap_still_routes(client, monkeypatch):
    # An entry code is longer than the 24-character barcode cap; it must route
    # as a grocycode, not be rejected as a concatenated scan.
    scanner_mode.set_mode("inventory")
    _stub_products(monkeypatch, [{"id": 123456, "name": "Big Batch Chili"}])

    async def fake_consume_entry(self, product_id, stock_entry_id, amount=1.0):
        return [{"best_before_date": "2026-08-01"}]

    monkeypatch.setattr(GrocyClient, "consume_stock_entry", fake_consume_entry)
    code = "grcy:p:123456:6a28c889c1193"
    assert len(code) > 24
    r = client.post("/pending/scan", json={"barcode": code})
    assert r.status_code == 200
    assert r.json()["status"] == "consumed"


def test_grocycode_product_only_form_consumes_by_product(client, monkeypatch):
    scanner_mode.set_mode("consume")
    _stub_products(monkeypatch, [{"id": 12, "name": "Chicken Stock"}])
    called = {}

    async def fake_consume(self, product_id, amount=1.0, spoiled=False):
        called.update(product_id=product_id, amount=amount)
        return [{"best_before_date": "2026-07-22"}]

    async def never(self, *a, **k):
        raise AssertionError("entry consume must not be used without an entry id")

    monkeypatch.setattr(GrocyClient, "consume_stock", fake_consume)
    monkeypatch.setattr(GrocyClient, "consume_stock_entry", never)
    r = client.post("/pending/scan", json={"barcode": "grcy:p:12", "quantity": 2})
    assert r.status_code == 200
    assert r.json()["status"] == "consumed"
    assert called == {"product_id": 12, "amount": 2.0}


def test_grocycode_audit_mode_counts_and_never_consumes(client, monkeypatch):
    from app.services import audit
    scanner_mode.set_mode("audit")
    _stub_products(monkeypatch, [{"id": 12, "name": "Chicken Stock"}])
    audit.start("Fridge", [{"name": "Chicken Stock", "amount": 1}])

    async def never(self, *a, **k):
        raise AssertionError("audit mode must never write to stock")

    monkeypatch.setattr(GrocyClient, "consume_stock_entry", never)
    monkeypatch.setattr(GrocyClient, "consume_stock", never)
    r = client.post("/pending/scan", json={"barcode": "grcy:p:12:6a28c889c1193"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "audit"
    assert body["status"] == "matched"
    assert body["name"] == "Chicken Stock"
    audit.reset()


def test_grocycode_unreadable_code_answers_cleanly(client, monkeypatch):
    scanner_mode.set_mode("inventory")

    async def never(self, *a, **k):
        raise AssertionError("an unreadable code must not reach Grocy")

    monkeypatch.setattr(GrocyClient, "consume_stock_entry", never)
    before = client.get("/pending/count").json()["count"]
    r = client.post("/pending/scan", json={"barcode": "grcy:p:junk:???"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "consume_failed"
    assert "could not be read" in body["error"].lower()
    # Nothing was queued as a pending row.
    assert client.get("/pending/count").json()["count"] == before


def test_grocycode_consume_failure_is_a_status_not_a_500(client, monkeypatch):
    scanner_mode.set_mode("inventory")
    _stub_products(monkeypatch, [{"id": 12, "name": "Chicken Stock"}])

    async def gone(self, product_id, stock_entry_id, amount=1.0):
        raise GrocyError("Grocy 400 on /stock/products/12/consume: not enough")

    monkeypatch.setattr(GrocyClient, "consume_stock_entry", gone)
    r = client.post("/pending/scan", json={"barcode": "grcy:p:12:6a28c889c1193"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "consume_failed"
    assert "Chicken Stock" in body["error"]


def test_grocycode_outage_reports_the_honest_reason(client, monkeypatch):
    scanner_mode.set_mode("inventory")
    _stub_products(monkeypatch, [{"id": 12, "name": "Chicken Stock"}])

    async def down(self, product_id, stock_entry_id, amount=1.0):
        raise GrocyError("Grocy is not reachable. Inventory will return when it is.")

    monkeypatch.setattr(GrocyClient, "consume_stock_entry", down)
    r = client.post("/pending/scan", json={"barcode": "grcy:p:12:6a28c889c1193"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "consume_failed"
    assert "not reachable" in body["error"]


def test_grocycode_consumes_even_when_name_lookup_fails(client, monkeypatch):
    scanner_mode.set_mode("inventory")

    async def products_down(self):
        raise GrocyError("Grocy is not reachable. Inventory will return when it is.")

    async def fake_consume_entry(self, product_id, stock_entry_id, amount=1.0):
        return [{"best_before_date": "2026-07-22"}]

    monkeypatch.setattr(GrocyClient, "get_products", products_down)
    monkeypatch.setattr(GrocyClient, "consume_stock_entry", fake_consume_entry)
    r = client.post("/pending/scan", json={"barcode": "grcy:p:12:6a28c889c1193"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "consumed"
    assert body["message"] == "Used up this item, best by Jul 22, 2026."


# -- Label endpoints attach the code ----------------------------------------


@pytest.fixture()
def print_client(tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    from app.config import settings
    from app.main import app
    from fastapi.testclient import TestClient
    saved = {k: getattr(settings, k) for k in (
        "data_dir", "grocy_base_url", "grocy_api_key", "vision_provider",
        "gemini_api_key", "auth_required", "auth_password", "printing_enabled",
        "label_printer_queue", "document_printer_queue")}
    settings.data_dir = str(tmp_path)
    settings.grocy_base_url = "http://grocy.test"
    settings.grocy_api_key = "k"
    settings.vision_provider = "gemini"
    settings.gemini_api_key = "k"
    settings.auth_required = False
    settings.auth_password = ""
    settings.printing_enabled = True
    settings.label_printer_queue = "Zebra"
    settings.document_printer_queue = ""
    try:
        with TestClient(app) as c:
            c._settings = settings
            yield c
    finally:
        for k, v in saved.items():
            setattr(settings, k, v)
        os.chdir(cwd)


def test_print_label_attaches_grocycode_for_a_stocked_product(print_client, monkeypatch):
    from app.services import printing as printing_service
    monkeypatch.setattr(printing_service, "printing_available", lambda: True)

    async def fake_item_for_id(product_id):
        return {"product_id": 12, "name": "Chicken Stock",
                "added_date": "2026-07-08 08:00:00",
                "best_before_date": "2026-07-22"}

    async def fake_get(self, path):
        assert path == "/stock/products/12/entries"
        return [{"id": 1, "stock_id": "6a28c889c1193", "amount": 1,
                 "row_created_timestamp": "2026-07-08 08:00:01"}]

    monkeypatch.setattr(printing_router, "_item_for_id", fake_item_for_id)
    monkeypatch.setattr(GrocyClient, "_get", fake_get)
    captured = {}

    def fake_render(spec, shape="rectangle"):
        captured["grocycode"] = spec.grocycode
        return printing_router.label_render.render_label(spec)

    monkeypatch.setattr(printing_router, "_render_label_image", fake_render)

    def fake_print(queue, data, *, options=None):
        return printing_service.PrintResult(ok=True, job_id="Zebra-1")

    monkeypatch.setattr(printing_service, "print_bytes", fake_print)
    r = print_client.post("/printing/label", json={"product_id": 12})
    assert r.status_code == 200
    assert captured["grocycode"] == "grcy:p:12:6a28c889c1193"


def test_print_label_free_typed_stays_plain(print_client, monkeypatch):
    from app.services import printing as printing_service
    monkeypatch.setattr(printing_service, "printing_available", lambda: True)
    captured = {}

    def fake_render(spec, shape="rectangle"):
        captured["grocycode"] = spec.grocycode
        return printing_router.label_render.render_label(spec)

    monkeypatch.setattr(printing_router, "_render_label_image", fake_render)

    def fake_print(queue, data, *, options=None):
        return printing_service.PrintResult(ok=True, job_id="Zebra-1")

    monkeypatch.setattr(printing_service, "print_bytes", fake_print)
    r = print_client.post("/printing/label", json={"name": "Leftover soup"})
    assert r.status_code == 200
    assert captured["grocycode"] == ""


def test_print_label_grocy_outage_still_prints_plain(print_client, monkeypatch):
    from app.services import printing as printing_service
    monkeypatch.setattr(printing_service, "printing_available", lambda: True)

    async def fake_item_for_id(product_id):
        return {"product_id": 12, "name": "Chicken Stock",
                "added_date": "2026-07-08 08:00:00",
                "best_before_date": "2026-07-22"}

    async def boom(self, path):
        raise GrocyError("Grocy is not reachable. Inventory will return when it is.")

    monkeypatch.setattr(printing_router, "_item_for_id", fake_item_for_id)
    monkeypatch.setattr(GrocyClient, "_get", boom)
    captured = {}

    def fake_render(spec, shape="rectangle"):
        captured["grocycode"] = spec.grocycode
        return printing_router.label_render.render_label(spec)

    monkeypatch.setattr(printing_router, "_render_label_image", fake_render)

    def fake_print(queue, data, *, options=None):
        return printing_service.PrintResult(ok=True, job_id="Zebra-1")

    monkeypatch.setattr(printing_service, "print_bytes", fake_print)
    r = print_client.post("/printing/label", json={"product_id": 12})
    assert r.status_code == 200
    assert captured["grocycode"] == ""


def test_batch_labels_each_carry_their_products_entry(print_client, monkeypatch):
    from app.services import printing as printing_service
    monkeypatch.setattr(printing_service, "printing_available", lambda: True)

    async def fake_stock(self):
        return [{"product_id": i, "name": f"Item {i}",
                 "added_date": "2026-07-08 08:00:00",
                 "best_before_date": "2026-07-22"} for i in (1, 2)]

    async def fake_get(self, path):
        # /stock/products/{pid}/entries
        pid = path.split("/")[3]
        return [{"id": 1, "stock_id": f"entry{pid}", "amount": 1,
                 "row_created_timestamp": "2026-07-08 08:00:01"}]

    monkeypatch.setattr(GrocyClient, "get_full_stock", fake_stock)
    monkeypatch.setattr(GrocyClient, "_get", fake_get)
    seen = []
    real_pdf = printing_router.label_render.render_batch_pdf_bytes

    def fake_pdf(specs, shape="rectangle"):
        seen.extend(s.grocycode for s in specs)
        return real_pdf(specs, shape=shape)

    monkeypatch.setattr(printing_router.label_render, "render_batch_pdf_bytes", fake_pdf)

    def fake_print(queue, data, *, options=None):
        return printing_service.PrintResult(ok=True, job_id="Zebra-2")

    monkeypatch.setattr(printing_service, "print_bytes", fake_print)
    r = print_client.post("/printing/label/batch", json={"product_ids": [1, 2]})
    assert r.status_code == 200
    assert r.json()["printed"] == 2
    assert seen == ["grcy:p:1:entry1", "grcy:p:2:entry2"]
