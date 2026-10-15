"""Grocy stock edits and moves never write from a stale row.

Grocy compacts a product's stock after every PUT /stock/entry: rows that then
agree on everything but the amount are merged into the row with the highest
id, amounts summed, and the rest are deleted. The quick best-by edit, the
sniff test, and the freezer move all built every write from one list read
before the first write, so a merge mid-loop turned the next write into a
stale restatement. On real Grocy 4.6.0 and 4.7.1 a sniff test and a quick edit
each took 5 units down to 3, freezing 1 + 1 + 2 units ended with 5 units and
2 of them still in the fridge, and a thaw left a freezer date on fridge stock.

FakeGrocy below applies Grocy's own rules, read from StockService.php and the
stock_splits and stock_next_use views of both versions, and the real
GrocyClient talks to it through the client's shared HTTP client.
"""
from __future__ import annotations

import asyncio
import copy
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "service"))

from app.config import settings  # noqa: E402
from app.services import grocy as grocy_svc  # noqa: E402
from app.services.defaults import (  # noqa: E402
    propose_transfer_best_by, storage_kind_for_bucket,
)
from app.services.grocy import GrocyClient  # noqa: E402

FRIDGE, FREEZER = 2, 3
SHARED_STOCK_ID = "6ab9687f0b750"


def _d(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def _next_use(row: dict) -> tuple:
    """Grocy's stock_next_use order: opened first, then earliest due, then
    earliest purchased (SQLite sorts a NULL first)."""
    return (-int(row["open"] or 0),
            row["best_before_date"] is not None, row["best_before_date"] or "",
            row["purchased_date"] is not None, row["purchased_date"] or "",
            row["id"])


class FakeGrocy:
    """Grocy's stock table, answering the client's HTTP calls.

    * GET /stock/products/{pid}/entries lists rows in stock_next_use order.
    * PUT /stock/entry/{id} restates the whole row (a field left out is
      written null), then compacts the product exactly as
      StockService::CompactStockEntries does: rows agreeing on best-by,
      purchase date, price, open, opened date, location, store and note merge
      into the highest id with the amounts summed, the smallest stock id
      survives, and every row carrying one of the other stock ids is renamed
      to it. A stock_id hash as the entry id is a 500, as on real Grocy.
    * POST /stock/products/{pid}/transfer walks the rows at the source in
      stock_next_use order, only those carrying ``stock_entry_id`` when the
      body names one, moving whole rows in place and splitting the last. It
      does not compact. Without a stock_entry_id it simply takes the earliest
      due rows, which is how the old code moved the wrong stock.
    """

    def __init__(self):
        self.rows: dict[int, dict] = {}
        self.locations = [{"id": FRIDGE, "name": "Refrigerator"},
                          {"id": FREEZER, "name": "Freezer"}]
        self.requests: list[tuple[str, str, dict | None]] = []
        self._stock_ids = 0

    def add(self, amount: float, best_before: str | None, location_id: int = FRIDGE,
            *, row_id: int | None = None, stock_id: str | None = None,
            price: float | None = None, note: str = "Perdue") -> dict:
        if row_id is None:
            row_id = max(self.rows, default=0) + 1
        if stock_id is None:
            self._stock_ids += 1
            stock_id = f"6ab9690a{self._stock_ids:05x}"
        self.rows[row_id] = {
            "id": row_id, "stock_id": stock_id, "product_id": 1,
            "amount": amount, "best_before_date": best_before,
            "location_id": location_id, "purchased_date": _d(-2),
            "price": price, "open": 0, "opened_date": None, "note": note,
            "shopping_location_id": 4,
        }
        return self.rows[row_id]

    # -- read back by the tests --------------------------------------------------

    def total(self) -> float:
        return sum(r["amount"] for r in self.rows.values())

    def at(self, location_id: int) -> list[tuple[str, float]]:
        return sorted((r["best_before_date"], r["amount"])
                      for r in self.rows.values() if r["location_id"] == location_id)

    def sent(self, method: str, suffix: str) -> list[dict]:
        return [b for m, p, b in self.requests if m == method and p.endswith(suffix)]

    def entry_writes(self) -> list[dict]:
        return [b for m, p, b in self.requests
                if m == "PUT" and p.startswith("/stock/entry/")]

    # -- the HTTP side -------------------------------------------------------------

    async def request(self, method, url, headers=None, json=None):
        path = httpx.URL(url).path.removeprefix("/api")
        self.requests.append((method, path, copy.deepcopy(json)))
        status, payload = self._answer(method, path, json)
        if payload is None:
            return httpx.Response(status)
        return httpx.Response(status, json=payload)

    def _answer(self, method, path, body):
        m = re.fullmatch(r"/stock/products/(\d+)/entries", path)
        if m and method == "GET":
            return 200, self._entries(int(m[1]))
        m = re.fullmatch(r"/stock/entry/([^/]+)", path)
        if m and method == "PUT":
            return self._edit(m[1], body)
        m = re.fullmatch(r"/stock/products/(\d+)/transfer", path)
        if m and method == "POST":
            return self._transfer(int(m[1]), body)
        if path == "/objects/locations" and method == "GET":
            return 200, self.locations
        if re.fullmatch(r"/objects/products/\d+", path) and method == "PUT":
            return 204, None
        return 400, {"error_message": f"FakeGrocy does not answer {method} {path}"}

    def _entries(self, product_id: int) -> list[dict]:
        rows = [r for r in self.rows.values()
                if r["product_id"] == product_id and r["amount"] > 0]
        return [dict(r) for r in sorted(rows, key=_next_use)]

    def _edit(self, raw_id: str, body: dict):
        if not raw_id.isdigit():
            return 500, {"error_message": "Grocy\\Services\\StockService::EditStockEntry(): "
                                          "Argument #1 ($stockRowId) must be of type int, "
                                          "string given"}
        row = self.rows.get(int(raw_id))
        if row is None:
            return 400, {"error_message": "Stock does not exist"}
        if "amount" not in body:
            return 400, {"error_message": "An amount is required"}

        def number(key):
            value = body.get(key)
            return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

        row.update({
            "amount": body["amount"],
            "best_before_date": body.get("best_before_date"),
            "location_id": number("location_id"),
            "shopping_location_id": number("shopping_location_id"),
            "price": number("price"),
            "open": 1 if body.get("open") else 0,
            "purchased_date": body.get("purchased_date"),
            "note": body.get("note"),
        })
        if not row["open"]:
            row["opened_date"] = None
        elif row["opened_date"] is None:
            row["opened_date"] = date.today().isoformat()
        self._compact(row["product_id"])
        return 200, [{"transaction_id": "t"}]

    def _compact(self, product_id: int) -> None:
        groups: dict[tuple, list[dict]] = {}
        for r in self.rows.values():
            if r["product_id"] != product_id or str(r["stock_id"]).startswith("x"):
                continue
            key = (r["best_before_date"], r["purchased_date"], r["price"], r["open"],
                   r["opened_date"], r["location_id"], r["shopping_location_id"],
                   r["note"] or "")
            groups.setdefault(key, []).append(r)
        for group in groups.values():
            if len(group) < 2:
                continue
            keep_stock_id = min(r["stock_id"] for r in group)
            keep_id = max(r["id"] for r in group)
            total = sum(r["amount"] for r in group)
            for other in {r["stock_id"] for r in group} - {keep_stock_id}:
                for r in self.rows.values():
                    if r["stock_id"] == other:
                        r["stock_id"] = keep_stock_id
            for r in group:
                if r["id"] != keep_id:
                    del self.rows[r["id"]]
            self.rows[keep_id]["amount"] = total

    def _transfer(self, product_id: int, body: dict):
        left = float(body["amount"])
        source = [r for r in self._entries(product_id)
                  if r["location_id"] == body["location_id_from"]]
        if left > sum(r["amount"] for r in source) + 1e-9:
            return 400, {"error_message": "Amount to be transferred cannot be > current "
                                          "stock amount at the source location"}
        if body.get("stock_entry_id"):
            source = [r for r in source if r["stock_id"] == body["stock_entry_id"]]
        for listed in source:
            if left <= 0:
                break
            row = self.rows[listed["id"]]
            if left >= row["amount"]:
                row["location_id"] = body["location_id_to"]
                left -= row["amount"]
            else:
                row["amount"] -= left
                new_id = max(self.rows) + 1
                self.rows[new_id] = dict(row, id=new_id, amount=left,
                                         location_id=body["location_id_to"])
                left = 0
        return 200, [{"transaction_id": "t"}]


@pytest.fixture
def grocy(monkeypatch):
    fake = FakeGrocy()
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "test-key", raising=False)
    monkeypatch.setattr(grocy_svc, "_client", fake)
    return fake


def plus_90(old: date, from_bucket: str) -> date:
    return old + timedelta(days=90)


def freezer_rule(old: date, from_bucket: str) -> date | None:
    return propose_transfer_best_by(old, storage_kind_for_bucket(from_bucket),
                                    "frozen", 365)


def fridge_rule(old: date, from_bucket: str) -> date | None:
    return propose_transfer_best_by(old, storage_kind_for_bucket(from_bucket),
                                    "refrigerated", 5)


def _run(coro):
    return asyncio.run(coro)


# --- (a) the sniff test ---------------------------------------------------------

def test_a_sniff_test_keeps_every_unit_and_moves_both_dates(grocy):
    grocy.add(2, _d(0), row_id=6)
    grocy.add(3, _d(3), row_id=7)
    result = _run(GrocyClient().extend_best_by(1, 3))
    assert grocy.total() == 5
    assert grocy.at(FRIDGE) == [(_d(3), 2), (_d(6), 3)]
    assert result == {"product_id": 1, "updated": 2, "new_best_by": _d(3)}
    # The rows were never priced, so no write invents a price, and the
    # purchase date, brand note and store ride along.
    writes = grocy.entry_writes()
    assert len(writes) == 2
    assert all("price" not in body for body in writes)
    assert all((body["purchased_date"], body["note"], body["shopping_location_id"])
               == (_d(-2), "Perdue", 4) for body in writes)


def test_a_sniff_test_from_expired_dates_merges_without_losing_units(grocy):
    # Two expired rows both extend from today and land on the same date:
    # merging into the row already extended sums them, and nothing restates it.
    grocy.add(2, _d(-4))
    grocy.add(3, _d(-1))
    result = _run(GrocyClient().extend_best_by(1, 3))
    assert grocy.total() == 5
    assert grocy.at(FRIDGE) == [(_d(3), 5)]
    assert result["updated"] == 2 and result["new_best_by"] == _d(3)


# --- (b) the quick best-by edit -------------------------------------------------

def test_b_a_quick_edit_onto_another_entrys_date_keeps_the_total(grocy):
    grocy.add(2, _d(4), price=3.49, note="Fage")
    grocy.add(3, _d(10), price=3.49, note="Fage")
    _run(GrocyClient().edit_product(1, best_before_date=_d(10)))
    assert grocy.total() == 5
    assert grocy.at(FRIDGE) == [(_d(10), 5)]
    (row,) = grocy.rows.values()
    assert (row["price"], row["note"], row["purchased_date"], row["shopping_location_id"]) \
        == (3.49, "Fage", _d(-2), 4)
    # Only the entry that was not on the target date is written.
    assert len(grocy.entry_writes()) == 1


# --- (c) freezing and (d) thawing ------------------------------------------------

def _stock_ids(grocy) -> list[str]:
    return [r["stock_id"] for r in sorted(grocy.rows.values(), key=lambda r: r["id"])]


def test_c_freezing_moves_every_unit_with_its_own_new_date(grocy):
    for amount, days in ((1, 0), (1, 3), (2, 6)):
        grocy.add(amount, _d(days))
    ids = _stock_ids(grocy)
    result = _run(GrocyClient().move_product(1, "frozen", propose_best_by=plus_90))

    assert grocy.total() == 4
    assert grocy.at(FRIDGE) == []
    assert grocy.at(FREEZER) == [(_d(90), 1), (_d(93), 1), (_d(96), 2)]
    # Each transfer names the row it just dated and moves exactly its amount.
    transfers = grocy.sent("POST", "/transfer")
    assert [(t["amount"], t["stock_entry_id"]) for t in transfers] \
        == [(1, ids[0]), (1, ids[1]), (2, ids[2])]
    assert all(t["location_id_from"] == FRIDGE and t["location_id_to"] == FREEZER
               for t in transfers)
    assert result["moved_amount"] == 4
    assert result["best_by_change"] == "extended"
    assert [u["new"] for u in result["best_by_updates"]] == [_d(90), _d(93), _d(96)]
    # Every date write lands before the transfer that moves that row.
    order = [(m, p) for m, p, _b in grocy.requests if m in ("PUT", "POST")]
    for n in (1, 2, 3):
        assert order.index(("PUT", f"/stock/entry/{n}")) < \
            [i for i, op in enumerate(order) if op[1].endswith("/transfer")][n - 1]


def test_d_thawing_leaves_no_freezer_date_on_fridge_stock(grocy):
    for amount, days in ((1, 0), (1, 3), (2, 6)):
        grocy.add(amount, _d(days))
    _run(GrocyClient().move_product(1, "frozen", propose_best_by=freezer_rule))
    # A later date write compacts the whole product, so rows that reached the
    # freezer with the same date may since have merged.
    assert grocy.at(FRIDGE) == []
    assert sum(amount for _day, amount in grocy.at(FREEZER)) == 4
    assert {day for day, _amount in grocy.at(FREEZER)} == {_d(365)}

    result = _run(GrocyClient().move_product(1, "refrigerated", propose_best_by=fridge_rule))
    assert grocy.total() == 4
    assert grocy.at(FREEZER) == []
    assert {day for day, _amount in grocy.at(FRIDGE)} == {_d(5)}
    assert result["best_by_change"] == "shortened"
    assert result["moved_amount"] == 4


# --- rows sharing a stock id ----------------------------------------------------

def test_e_rows_sharing_a_stock_id_each_get_their_own_date_before_moving(grocy):
    """A merge renames stock ids, so rows at one location can share one, and a
    transfer naming it takes them earliest due first. Dating only the row
    about to move pushed it behind its twin, and the twin went into the
    freezer still carrying its fridge date."""
    grocy.add(2, _d(2), stock_id=SHARED_STOCK_ID)
    grocy.add(1, _d(8), stock_id=SHARED_STOCK_ID)
    result = _run(GrocyClient().move_product(1, "frozen", propose_best_by=plus_90))
    assert grocy.total() == 3
    assert grocy.at(FRIDGE) == []
    # Each row dated exactly once, from its own date.
    assert grocy.at(FREEZER) == [(_d(92), 2), (_d(98), 1)]
    assert result["moved_amount"] == 3


def test_f_freezing_what_the_old_thaw_left_behind(grocy):
    """The fridge the old code left after a freeze and a thaw (reproduced on
    Grocy 4.6.0 and 4.7.1): three rows sharing one stock id, one of them still
    carrying the freezer date."""
    grocy.add(2, _d(365), stock_id=SHARED_STOCK_ID)
    grocy.add(1, _d(5), stock_id=SHARED_STOCK_ID)
    grocy.add(2, _d(5), stock_id=SHARED_STOCK_ID)
    _run(GrocyClient().move_product(1, "frozen", propose_best_by=freezer_rule))
    assert grocy.total() == 5
    assert grocy.at(FRIDGE) == []
    assert {day for day, _amount in grocy.at(FREEZER)} == {_d(365)}


def test_a_row_already_on_the_new_date_still_gets_its_own_date(grocy):
    """A row with other details (here a price) can already sit on the date the
    first row is given. The two do not merge, so the first row is still the
    one to move, and the other keeps its turn to be dated from its own date."""
    grocy.add(1, _d(0))
    grocy.add(1, _d(90), price=2.0)
    result = _run(GrocyClient().move_product(1, "frozen", propose_best_by=plus_90))
    assert grocy.total() == 2
    assert grocy.at(FRIDGE) == []
    assert grocy.at(FREEZER) == [(_d(90), 1), (_d(180), 1)]
    assert [u["new"] for u in result["best_by_updates"]] == [_d(90), _d(180)]


def test_an_opened_row_and_its_unopened_twin_both_move_with_new_dates(grocy):
    # Opening part of a row splits it: both halves keep the stock id, and
    # Grocy hands out the opened one first.
    grocy.add(1, _d(4), stock_id=SHARED_STOCK_ID)
    opened = grocy.add(1, _d(2), stock_id=SHARED_STOCK_ID)
    opened["open"] = 1
    _run(GrocyClient().move_product(1, "frozen", propose_best_by=freezer_rule))
    assert grocy.total() == 2
    assert grocy.at(FRIDGE) == []
    assert grocy.at(FREEZER) == [(_d(365), 1), (_d(365), 1)]


# --- the rest of the move contract ------------------------------------------------

def test_a_move_without_a_proposer_names_each_row_and_touches_no_date(grocy):
    grocy.add(1, _d(3))
    grocy.add(2, _d(6))
    ids = _stock_ids(grocy)
    result = _run(GrocyClient().move_product(1, "frozen"))
    assert grocy.at(FREEZER) == [(_d(3), 1), (_d(6), 2)]
    assert grocy.entry_writes() == []
    assert [t["stock_entry_id"] for t in grocy.sent("POST", "/transfer")] == ids
    assert result["moved_amount"] == 3 and result["best_by_updates"] == []


def test_a_row_without_a_stock_id_moves_without_naming_one(grocy):
    # A very old Grocy lists no stock id; the transfer then takes the rows at
    # the source in order, one whole row at a time.
    grocy.add(1, _d(3), stock_id="")
    grocy.add(2, _d(6), stock_id="")
    _run(GrocyClient().move_product(1, "frozen"))
    transfers = grocy.sent("POST", "/transfer")
    assert [t["amount"] for t in transfers] == [1, 2]
    assert not any("stock_entry_id" in t for t in transfers)
    assert grocy.at(FREEZER) == [(_d(3), 1), (_d(6), 2)]


def test_a_row_without_a_location_is_left_for_the_default_location(grocy):
    grocy.add(1, _d(3), location_id=None)
    result = _run(GrocyClient().move_product(1, "frozen", propose_best_by=plus_90))
    assert grocy.sent("POST", "/transfer") == []
    assert result["best_by_updates"] == [] and result["moved_amount"] == 0
    assert grocy.sent("PUT", "/objects/products/1") == [{"location_id": FREEZER}]


def test_a_stock_id_hash_is_never_sent_as_the_entry_id(grocy):
    # Grocy answers PUT /stock/entry/<hash> with a 500 TypeError.
    row = grocy.add(1, _d(3))
    for not_a_row_id in ("", None, row["stock_id"]):
        assert _run(GrocyClient()._set_entry_best_by(1, not_a_row_id, _d(9))) is False
    assert grocy.entry_writes() == []


def test_a_write_skips_a_row_that_is_gone_or_already_on_the_date(grocy):
    grocy.add(1, _d(3))
    assert _run(GrocyClient()._set_entry_best_by(1, 99, _d(9))) is False
    assert _run(GrocyClient()._set_entry_best_by(1, 1, _d(3))) is False
    assert grocy.entry_writes() == []
    assert _run(GrocyClient()._set_entry_best_by(1, 1, _d(9))) is True
    assert grocy.at(FRIDGE) == [(_d(9), 1)]
