"""Per-date Inventory rows act on exactly their own stock.

With one Inventory row per best-by date, a row's Use one, Mark all consumed,
Mark one opened, Edit and Move must touch only the stock with that date in
that place. Grocy takes stock by its stock id, a hash several rows can share
(a partial transfer copies it, a merge renames rows to it), and walks the rows
in the order /stock/products/{id}/entries lists them. So a per-date action
names one row at a time, only a row Grocy would take first for its stock id,
by that row's exact amount, and refuses before writing anything when the
stock it needs sits behind other stock.

FakeGrocy471 extends the FakeGrocy of test_grocy_stock_edits.py with Grocy
4.7.1's own consume, open and transfer, ported from StockService.php. The
shapes below are the ones probed on real Grocy 4.6.0 and 4.7.1, which behaved
the same: undated stock is walked first, the default consume location comes
before any other place, a partial open gives the unopened rest a new stock
id, and every body number is read at 14 significant digits, so one consume of
0.1 + 0.2 across two rows leaves a 2.8e-17 crumb behind.
"""
from __future__ import annotations

import asyncio
import re
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import settings  # noqa: E402
from app.services import grocy as grocy_svc  # noqa: E402
from app.services.grocy import (  # noqa: E402
    BEST_BY_ROW_GONE, BEST_BY_ROW_TARE, GrocyClient, GrocyError, best_by_group_blocker,
    best_by_group_head, best_by_group_reach, best_by_label_stock_id, best_by_take,
    in_best_by_group,
)
from test_grocy_stock_edits import FREEZER, FRIDGE, FakeGrocy, _d, plus_90  # noqa: E402

CELLAR = 4
DUST = 2.7755575615629e-17   # what one consume across a 0.1 and a 0.2 row leaves


def _grocy_number(value):
    """A value the way Grocy reads it from a body and writes it in an answer:
    every float at PHP's precision of 14 significant digits."""
    if isinstance(value, float):
        return float(f"{value:.14g}")
    if isinstance(value, dict):
        return {k: _grocy_number(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_grocy_number(v) for v in value]
    return value


class FakeGrocy471(FakeGrocy):
    """FakeGrocy with Grocy 4.7.1's consume, open and transfer.

    * Body numbers are read the way Grocy reads them, as text at 14
      significant digits (0.30000000000000004 is booked as 0.3), and answers
      write their numbers back the same way.
    * GET .../entries lists rows in Grocy's whole walk order: the product's
      default consume location first, then opened, then best-by and purchase
      date (no date first), then amount and id, the index Grocy reads.
    * consume, open and transfer follow StockService.php line by line: a row
      is taken whole while the amount left is >= its own, the walk stops only
      at exactly 0, asking for more than the pool holds books less and still
      answers 200, and when nothing was booked the answer is 400 "No
      transaction was found by the given transaction id". Each answers with
      the stock_log rows it wrote, each with the place and date the stock had
      before (a transfer_to row: after Grocy's own freezer rule).
    * A partial open leaves the opened part on the row (same id and stock id,
      the date pulled in to today plus the product's days after opening,
      never later) and gives the rest a new row and a new stock id. A partial
      transfer leaves the rest at the source and lands a new row there with
      the same stock id. With move_on_open the opened unit then moves to the
      default consume location.
    * PUT /stock/entry compacts as FakeGrocy does: MAX(id) and MIN(stock_id)
      survive, and the dropped stock id is renamed on every row.

    ``before_write`` and ``after_write`` stand in for another device: called
    with (method, path, body) around each write Grocy handles.
    """

    def __init__(self):
        super().__init__()
        self.locations = [{"id": FRIDGE, "name": "Refrigerator", "is_freezer": 0},
                          {"id": FREEZER, "name": "Freezer", "is_freezer": 1},
                          {"id": CELLAR, "name": "Basement Fridge", "is_freezer": 0}]
        self.product = {"id": 1, "name": "Coconut Yogurt", "location_id": FRIDGE,
                        "default_consume_location_id": None, "move_on_open": 0,
                        "enable_tare_weight_handling": 0,
                        "default_best_before_days_after_open": 3,
                        "default_best_before_days_after_freezing": 0,
                        "default_best_before_days_after_thawing": 0}
        self.log: list[dict] = []
        self.before_write = None
        self.after_write = None
        self._serial = 0
        self._last_row_id = 0

    def add(self, amount, best_before, location_id=FRIDGE, *, opened=False,
            purchased=None, **kwargs):
        row = super().add(amount, best_before, location_id, **kwargs)
        if opened:
            row.update(open=1, opened_date=_d(-1))
        if purchased is not None:
            row["purchased_date"] = purchased
        return row

    def at(self, location_id):
        return sorted(((r["best_before_date"], r["amount"]) for r in self.rows.values()
                       if r["location_id"] == location_id),
                      key=lambda p: (p[0] is None, p[0] or "", p[1]))

    def writes(self):
        return [(m, p) for m, p, _b in self.requests if m != "GET"]

    # -- the HTTP side -------------------------------------------------------------

    def _answer(self, method, path, body):
        body = _grocy_number(body)
        if method != "GET" and self.before_write is not None:
            self.before_write(method, path, body)
        m = re.fullmatch(r"/stock/products/(\d+)/(consume|open)", path)
        if m and method == "POST":
            handler = self._consume if m[2] == "consume" else self._open
            status, payload = handler(int(m[1]), body)
        elif path == "/objects/products" and method == "GET":
            status, payload = 200, [dict(self.product)]
        elif path == "/objects/locations" and method == "POST":
            new_id = max(loc["id"] for loc in self.locations) + 1
            self.locations.append({"id": new_id, "name": body["name"], "is_freezer": 0})
            status, payload = 200, {"created_object_id": new_id}
        else:
            status, payload = super()._answer(method, path, body)
        if method != "GET" and self.after_write is not None:
            self.after_write(method, path, body)
        return status, _grocy_number(payload)

    def _walk_key(self, row):
        default = self.product.get("default_consume_location_id")
        return (0 if default is not None and row["location_id"] == default else 1,
                -int(row["open"] or 0),
                row["best_before_date"] is not None, row["best_before_date"] or "",
                row["purchased_date"] is not None, row["purchased_date"] or "",
                row["amount"], row["id"])

    def _entries(self, product_id):
        rows = sorted((r for r in self.rows.values()
                       if r["product_id"] == product_id and r["amount"] > 0),
                      key=self._walk_key)
        return [dict(r, priority=-n) for n, r in enumerate(rows, 1)]

    def _new_serial(self, prefix):
        self._serial += 1
        return f"{prefix}{self._serial:04d}"

    def _new_row_id(self):
        # Grocy's stock ids autoincrement and are never handed out twice.
        self._last_row_id = max(self._last_row_id, max(self.rows, default=0)) + 1
        return self._last_row_id

    def _new_stock_id(self):
        self._stock_ids += 1
        return f"6ab9690a{self._stock_ids:05x}"

    def _book(self, tid, row, amount, kind, **changes):
        self.log.append({
            "id": len(self.log) + 1, "product_id": row["product_id"], "amount": amount,
            "best_before_date": row["best_before_date"],
            "purchased_date": row["purchased_date"], "stock_id": row["stock_id"],
            "transaction_type": kind, "price": row["price"], "undone": 0,
            "opened_date": row["opened_date"], "location_id": row["location_id"],
            "correlation_id": None, "transaction_id": tid, "stock_row_id": None,
            "shopping_location_id": row["shopping_location_id"], "spoiled": 0,
            "note": row["note"], **changes})

    def _booked(self, tid):
        rows = [dict(r) for r in self.log if r["transaction_id"] == tid]
        if not rows:
            return 400, {"error_message": "No transaction was found by the given transaction id"}
        return 200, rows

    def _consume(self, product_id, body):
        amount = body["amount"]
        if amount <= 0:
            return 400, {"error_message": "Amount can't be <= 0"}
        stock = [r for r in self.rows.values() if r["product_id"] == product_id]
        pool = [self.rows[e["id"]] for e in self._entries(product_id)]
        if body.get("location_id"):
            pool = [r for r in pool if r["location_id"] == body["location_id"]]
        if body.get("stock_entry_id"):
            pool = [r for r in pool if r["stock_id"] == body["stock_entry_id"]]
        if round(amount, 2) > round(sum(r["amount"] for r in stock), 2):
            return 400, {"error_message": "Amount to be consumed cannot be > current stock "
                                          "amount (if supplied, at the desired location)"}
        tid = self._new_serial("t")
        spoiled = 1 if body.get("spoiled") else 0
        for row in pool:
            if amount == 0:
                break
            if amount >= row["amount"]:
                self._book(tid, row, row["amount"] * -1, "consume", spoiled=spoiled)
                del self.rows[row["id"]]
                amount -= row["amount"]
            else:
                self._book(tid, row, amount * -1, "consume", spoiled=spoiled)
                row["amount"] = row["amount"] - amount
                amount = 0
        return self._booked(tid)

    def _open(self, product_id, body):
        amount = body["amount"]
        product = self.product
        stock = [r for r in self.rows.values() if r["product_id"] == product_id]
        unopened = sum(r["amount"] for r in stock) - sum(r["amount"] for r in stock if r["open"])
        if product.get("enable_tare_weight_handling"):
            return 400, {"error_message": "Opening tare weight handling enabled products "
                                          "is not supported"}
        if amount > unopened:
            return 400, {"error_message": "Amount to be opened cannot be > current unopened "
                                          "stock amount"}
        pool = [self.rows[e["id"]] for e in self._entries(product_id) if not e["open"]]
        if body.get("stock_entry_id"):
            pool = [r for r in pool if r["stock_id"] == body["stock_entry_id"]]
        tid = self._new_serial("t")
        today = date.today().isoformat()
        days = product.get("default_best_before_days_after_open") or 0
        for row in pool:
            if amount == 0:
                break
            new_date = row["best_before_date"]
            if days > 0:
                new_date = _d(days)
                # Never later than the old date. strtotime(NULL) is false, so
                # undated stock stays undated.
                if row["best_before_date"] is None or new_date > row["best_before_date"]:
                    new_date = row["best_before_date"]
            if amount >= row["amount"]:
                self._book(tid, row, row["amount"], "product-opened", opened_date=today)
                row.update(open=1, opened_date=today, best_before_date=new_date)
                amount -= row["amount"]
            else:
                rest_id = self._new_row_id()
                self.rows[rest_id] = dict(row, id=rest_id, amount=row["amount"] - amount,
                                          stock_id=self._new_stock_id(), open=0,
                                          opened_date=None)
                self._book(tid, row, amount, "product-opened", opened_date=today)
                row.update(amount=amount, open=1, opened_date=today, best_before_date=new_date)
                amount = 0
            to = product.get("default_consume_location_id")
            if product.get("move_on_open") and to and to != row["location_id"]:
                self._transfer_rows(product_id, row["amount"], row["location_id"], to,
                                    row["stock_id"], tid)
        return self._booked(tid)

    def _transfer(self, product_id, body):
        if self.product.get("enable_tare_weight_handling"):
            return 400, {"error_message": "Transferring tare weight enabled products is "
                                          "not yet possible"}
        amount, from_id = body["amount"], body["location_id_from"]
        at_source = sum(r["amount"] for r in self.rows.values()
                        if r["product_id"] == product_id and r["location_id"] == from_id)
        if amount > at_source:
            return 400, {"error_message": "Amount to be transferred cannot be > current "
                                          "stock amount at the source location"}
        tid = self._new_serial("t")
        self._transfer_rows(product_id, amount, from_id, body["location_id_to"],
                            body.get("stock_entry_id") or None, tid)
        return self._booked(tid)

    def _transfer_rows(self, product_id, amount, from_id, to_id, stock_id, tid):
        pool = [self.rows[e["id"]] for e in self._entries(product_id)
                if e["location_id"] == from_id]
        if stock_id:
            pool = [r for r in pool if r["stock_id"] == stock_id]
        for row in pool:
            if amount == 0:
                break
            arrival = self._arrival_date(row, from_id, to_id)
            pair = self._new_serial("c")
            moving = row["amount"] if amount >= row["amount"] else amount
            self._book(tid, row, moving * -1, "transfer_from", correlation_id=pair)
            self._book(tid, row, moving, "transfer_to", correlation_id=pair,
                       location_id=to_id, best_before_date=arrival)
            if amount >= row["amount"]:
                row.update(location_id=to_id, best_before_date=arrival)
                amount -= row["amount"]
            else:
                row["amount"] = row["amount"] - amount
                new_id = self._new_row_id()
                self.rows[new_id] = dict(row, id=new_id, amount=amount, location_id=to_id,
                                         best_before_date=arrival)
                amount = 0

    def _arrival_date(self, row, from_id, to_id):
        """Grocy's own freezer rule: freezing and thawing days of the product,
        for locations marked as freezers in Grocy."""
        freezer = {loc["id"]: bool(loc.get("is_freezer")) for loc in self.locations}
        frozen = self.product.get("default_best_before_days_after_freezing") or 0
        thawed = self.product.get("default_best_before_days_after_thawing") or 0
        if not freezer.get(from_id) and freezer.get(to_id) and (frozen > 0 or frozen == -1):
            return "2999-12-31" if frozen == -1 else _d(frozen)
        if freezer.get(from_id) and not freezer.get(to_id) and thawed > 0:
            return _d(thawed)
        return row["best_before_date"]


@pytest.fixture
def grocy(monkeypatch):
    fake = FakeGrocy471()
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "test-key", raising=False)
    monkeypatch.setattr(grocy_svc, "_client", fake)
    return fake


def _run(coro):
    return asyncio.run(coro)


def _refused(coro) -> GrocyError:
    with pytest.raises(GrocyError) as caught:
        _run(coro)
    return caught.value


# --- The fake is Grocy ----------------------------------------------------------

def test_the_fake_leaves_grocys_crumb_behind_a_summed_consume(grocy):
    """The probe that started the one-row-at-a-time rule: today's location
    consume of 0.1 + 0.2 books both rows and still leaves 2.8e-17 behind."""
    grocy.add(0.1, _d(5), purchased=_d(-3))
    grocy.add(0.2, _d(5))
    booked = _run(GrocyClient().consume_stock(1, 0.1 + 0.2, location_id=FRIDGE))
    assert [r["amount"] for r in booked] == [-0.1, -0.2]
    (crumb,) = grocy.rows.values()
    assert crumb["amount"] == pytest.approx(DUST)


def test_the_fake_walks_stock_the_way_grocy_does(grocy):
    grocy.product["default_consume_location_id"] = FREEZER
    later_in_the_freezer = grocy.add(1, _d(60), FREEZER)
    undated = grocy.add(1, None)
    opened = grocy.add(1, _d(9), opened=True)
    big = grocy.add(3, _d(5))
    small = grocy.add(1, _d(5))
    order = [e["id"] for e in _run(GrocyClient()._get("/stock/products/1/entries"))]
    assert order == [later_in_the_freezer["id"], opened["id"], undated["id"],
                     small["id"], big["id"]]


# --- Which stock a row is -------------------------------------------------------

A, B = 3, 4                                   # two places, as in the probes
D0, D1, D2, D3 = "2026-10-23", "2026-10-25", "2026-11-02", "2026-11-20"


def _row(row_id, stock_id, location_id, amount, best_before, *, opened=0):
    return {"id": row_id, "product_id": 1, "stock_id": stock_id,
            "location_id": location_id, "amount": amount,
            "best_before_date": best_before, "open": opened,
            "purchased_date": "2026-10-20"}


def test_a_row_is_one_place_and_one_best_by_date():
    entry = _row(1, "s1", A, 2, D1)
    assert in_best_by_group(entry, A, D1)
    assert in_best_by_group(entry, str(A), D1)
    assert not in_best_by_group(entry, B, D1)
    assert not in_best_by_group(entry, A, D2)
    assert not in_best_by_group(entry, A, "none")
    # "none" is the row of undated stock, however Grocy spells it.
    assert in_best_by_group(_row(2, "s2", A, 1, None), A, "none")
    assert in_best_by_group(_row(3, "s3", A, 1, ""), A, "none")
    # Dust and emptied rows are no stock at all.
    assert not in_best_by_group(_row(4, "s4", A, DUST, D1), A, D1)
    assert not in_best_by_group(_row(5, "s5", A, 0, D1), A, D1)


def test_a_stock_id_shared_with_an_earlier_date_blocks_the_later_row():
    """Probe f2: one stock id on an earlier row and a later one in the same
    place. Naming it takes the earlier row first."""
    earlier = _row(43, "d761", A, 1, D0)
    later = _row(40, "d761", A, 1, D2)
    own = _row(41, "e002", A, 2, D2)
    entries = [earlier, later, own]
    assert best_by_group_head(entries, A, D2) is own
    assert best_by_group_head(entries, A, D0) is earlier
    assert best_by_group_reach(entries, A, D2) == 2       # the shared one is out of reach
    assert best_by_group_blocker(entries, A, D2) is earlier


def test_a_consume_walks_one_place_but_an_open_walks_the_whole_product():
    """Probes f4 and f5: with one stock id in two places, a consume naming the
    place takes that place's row, while an open takes the row Grocy lists
    first, wherever it is."""
    elsewhere = _row(43, "d761", B, 1, D0)
    here = _row(40, "d761", A, 1, D2)
    entries = [elsewhere, here]
    assert best_by_group_head(entries, A, D2) is here
    assert best_by_group_head(entries, A, D2, unopened=True) is None
    assert best_by_group_blocker(entries, A, D2, unopened=True) is elsewhere


def test_an_opened_twin_blocks_a_use_but_not_an_open():
    """Before Grocy 4.4 a partial open left the unopened rest on the opened
    unit's stock id. Grocy uses the opened unit first, but opens the rest."""
    twin = _row(18, "bbcae", A, 1, D0, opened=1)
    rest = _row(21, "bbcae", A, 2, D2)
    entries = [twin, rest]
    assert best_by_group_head(entries, A, D2) is None
    assert best_by_group_blocker(entries, A, D2) is twin
    assert best_by_group_head(entries, A, D2, unopened=True) is rest


def test_undated_stock_is_walked_first():
    """Probe f5: Grocy sorts a missing date first and uses it first."""
    undated = _row(114, "c992c", A, 1, None)
    dated = _row(113, "c992c", A, 1, D0)
    entries = [undated, dated]
    assert best_by_group_head(entries, A, "none") is undated
    assert best_by_group_head(entries, A, D0) is None
    assert best_by_group_blocker(entries, A, D0) is undated


def test_the_default_consume_location_comes_first_for_an_open():
    """Probe d: rows at the product's default consume location are listed,
    and opened, before an earlier date anywhere else."""
    at_default = _row(70, "aa01", B, 1, D3)
    here = _row(66, "aa01", A, 2, D1)
    entries = [at_default, here]          # /entries order with B as the default
    assert best_by_group_head(entries, A, D1, unopened=True) is None
    assert best_by_group_blocker(entries, A, D1, unopened=True) is at_default
    # A consume names the place, and walks only that place.
    assert best_by_group_head(entries, A, D1) is here


def test_dust_and_rows_without_a_stock_id_are_never_named():
    crumb = _row(116, "cafb", A, DUST, D0)
    row = _row(117, "cafb", A, 1, D1)
    assert best_by_group_head([crumb, row], A, D1) is row
    assert best_by_group_head([crumb, row], A, D0) is None
    assert best_by_group_reach([crumb, row], A, D0) == 0
    assert best_by_group_head([_row(1, "", A, 1, D1), _row(2, None, A, 1, D1)], A, D1) is None


def test_taking_a_row_can_put_another_date_first_for_its_stock_id():
    """An opened row of the group, an opened row of a later date, and an
    unopened row of the group, all on one stock id: once the first is used,
    Grocy would take the later date's opened row next."""
    first = _row(1, "s9", A, 1, D1, opened=1)
    later = _row(2, "s9", A, 1, D2, opened=1)
    last = _row(3, "s9", A, 2, D1)
    entries = [first, later, last]
    assert best_by_group_head(entries, A, D1) is first
    assert best_by_group_reach(entries, A, D1) == 1
    assert best_by_group_reach(entries, A, D1, 0.5) == 0.5


@pytest.mark.parametrize("amounts", [(0.1, 0.2), (0.7, 0.1), (0.7, 0.2, 0.1)])
def test_fractional_rows_are_reached_whole_without_a_false_refusal(amounts):
    entries = [_row(n, f"s{n}", A, amount, D1) for n, amount in enumerate(amounts, 1)]
    total = sum(amounts)
    assert best_by_group_reach(entries, A, D1) == pytest.approx(total)
    assert best_by_take(entries, A, D1, total) == total
    assert best_by_take(entries, A, D1, 99) == total


def test_a_take_is_worked_out_before_anything_is_written():
    entries = [_row(1, "s1", A, 0.5, D1), _row(2, "s2", A, 3, D2)]
    # Use one on half a unit uses the half; Mark all takes what is there now.
    assert best_by_take(entries, A, D1, 1) == 0.5
    assert best_by_take(entries, A, D2, 1) == 1
    assert best_by_take(entries, A, D2, 5) == 3
    gone = pytest.raises(GrocyError, best_by_take, entries, A, D3, 1).value
    assert (gone.kind, str(gone)) == ("gone", BEST_BY_ROW_GONE)
    blocked = pytest.raises(GrocyError, best_by_take,
                            [_row(3, "s3", A, 1, D0), _row(4, "s3", A, 1, D1)], A, D1, 1).value
    assert blocked.kind == "group"
    assert str(blocked) == ("Grocy would use one with an earlier best-by date first, "
                            "so nothing was used.")


def test_a_rows_label_codes_a_stock_id_only_that_row_carries():
    rows = [
        {"id": 1, "stock_id": "s1", "location_id": A, "amount": 1, "best_before_date": D1,
         "row_created_timestamp": "2026-10-20 09:00:00"},
        {"id": 2, "stock_id": "s2", "location_id": A, "amount": 1, "best_before_date": D1,
         "row_created_timestamp": "2026-10-21 09:00:00"},
        # s2 also sits on a row in another place, so a scan could take that one.
        {"id": 3, "stock_id": "s2", "location_id": B, "amount": 1, "best_before_date": D1,
         "row_created_timestamp": "2026-10-21 09:00:00"},
        {"id": 4, "stock_id": "s4", "location_id": A, "amount": 1, "best_before_date": D2},
    ]
    assert best_by_label_stock_id(rows, A, D1) == "s1"
    # When every stock id is shared, the newest row's still beats none.
    rows[0]["stock_id"] = "s4"
    assert best_by_label_stock_id(rows, A, D1) == "s2"
    assert best_by_label_stock_id(rows, A, D3) is None


# --- Use one and Mark all consumed ----------------------------------------------

def test_use_one_takes_the_unit_from_the_rows_own_entry(grocy):
    grocy.add(2, _d(3))                 # what a consume with just the place takes
    row = grocy.add(3, _d(30))
    result = _run(GrocyClient().consume_stock_group(1, 1, FRIDGE, _d(30)))
    assert grocy.sent("POST", "/consume") == [
        {"amount": 1, "spoiled": False, "location_id": FRIDGE,
         "stock_entry_id": row["stock_id"]}]
    assert grocy.at(FRIDGE) == [(_d(3), 2), (_d(30), 2)]
    assert result == {"product_id": 1, "consumed": 1, "requested": 1, "complete": True}


def test_mark_all_takes_each_entry_of_the_row_whole_and_nothing_else(grocy):
    grocy.add(1, _d(2))                           # an earlier date in the same place
    small = grocy.add(2, _d(9), price=1.5)        # the row: two entries Grocy keeps apart
    big = grocy.add(3, _d(9), price=2.0)
    grocy.add(4, _d(9), FREEZER)                  # the same date in another place
    result = _run(GrocyClient().consume_stock_group(1, 5, FRIDGE, _d(9), spoiled=True))
    assert [(c["amount"], c["stock_entry_id"], c["spoiled"])
            for c in grocy.sent("POST", "/consume")] == [
        (2, small["stock_id"], True), (3, big["stock_id"], True)]
    assert grocy.at(FRIDGE) == [(_d(2), 1)]
    assert grocy.at(FREEZER) == [(_d(9), 4)]
    assert result == {"product_id": 1, "consumed": 5, "requested": 5, "complete": True}
    assert {(r["location_id"], r["best_before_date"], r["spoiled"]) for r in grocy.log} == {
        (FRIDGE, _d(9), 1)}


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("amounts", [(0.1, 0.2), (0.7, 0.1), (0.7, 0.2)])
def test_mark_all_on_fractional_entries_leaves_no_crumb(grocy, amounts, shared):
    """Sent as one summed amount, two rows of 0.1 and 0.2 sharing a stock id
    are booked in full and still leave 2.8e-17 behind, a "0.0" row."""
    grocy.add(5, _d(20))                                 # a later date stays
    stock_id = "6ab9690b00000" if shared else None
    for n, amount in enumerate(amounts):
        grocy.add(amount, _d(4), purchased=_d(-5 + n), stock_id=stock_id)
    page_total = sum(amounts)
    result = _run(GrocyClient().consume_stock_group(1, page_total, FRIDGE, _d(4)))
    # One call per row, each by the row's own amount.
    assert sorted(c["amount"] for c in grocy.sent("POST", "/consume")) == sorted(amounts)
    assert grocy.at(FRIDGE) == [(_d(20), 5)]
    assert result["complete"] is True
    assert result["consumed"] == pytest.approx(page_total)


def test_use_one_on_half_a_unit_uses_the_half(grocy):
    grocy.add(2, _d(2))
    half = grocy.add(0.5, _d(6))
    result = _run(GrocyClient().consume_stock_group(1, 1, FRIDGE, _d(6)))
    assert grocy.sent("POST", "/consume") == [
        {"amount": 0.5, "spoiled": False, "location_id": FRIDGE,
         "stock_entry_id": half["stock_id"]}]
    assert grocy.at(FRIDGE) == [(_d(2), 2)]
    assert result == {"product_id": 1, "consumed": 0.5, "requested": 0.5, "complete": True}


@pytest.mark.parametrize("stored", [0.3 - 0.1, 0.1 * 3])
def test_grocys_own_float_noise_never_spills_onto_another_date(grocy, stored):
    """A row Grocy left at 0.19999999999999998 (or 0.30000000000000004) is
    listed as 0.2 (0.3). Taking it by that amount books, at most, a crumb
    from the next row with its stock id, and that is not stock."""
    row = grocy.add(stored, _d(4))
    later = grocy.add(1, _d(9), stock_id=row["stock_id"], price=1.0)
    result = _run(GrocyClient().consume_stock_group(1, 1, FRIDGE, _d(4)))
    assert result["complete"] is True
    assert result["consumed"] == pytest.approx(stored)
    assert later["amount"] == pytest.approx(1)
    left = [r for r in grocy.rows.values() if r["best_before_date"] == _d(4)]
    assert all(r["amount"] < 1e-9 for r in left)


def test_a_use_grocy_would_take_from_an_earlier_date_writes_nothing(grocy):
    earlier = grocy.add(1, _d(2))
    grocy.add(2, _d(9), stock_id=earlier["stock_id"], price=1.0)
    refused = _refused(GrocyClient().consume_stock_group(1, 1, FRIDGE, _d(9)))
    assert refused.kind == "group"
    assert str(refused) == ("Grocy would use one with an earlier best-by date first, "
                            "so nothing was used.")
    assert grocy.writes() == []


def test_a_use_grocy_would_take_from_an_opened_unit_writes_nothing(grocy):
    opened = grocy.add(1, _d(20), opened=True)
    grocy.add(3, _d(9), stock_id=opened["stock_id"])
    refused = _refused(GrocyClient().consume_stock_group(1, 2, FRIDGE, _d(9)))
    assert (refused.kind, str(refused)) == (
        "group", "Grocy would use an opened one first, so nothing was used.")
    assert grocy.writes() == []


def test_a_use_on_undated_stock_behind_its_own_undated_twin(grocy):
    """Undated stock is walked first, so an undated row sharing the stock id
    blocks a dated row, and the message names no date it does not have."""
    undated = grocy.add(1, None)
    grocy.add(2, _d(9), stock_id=undated["stock_id"])
    refused = _refused(GrocyClient().consume_stock_group(1, 1, FRIDGE, _d(9)))
    assert str(refused) == "Grocy would use other stock first, so nothing was used."
    result = _run(GrocyClient().consume_stock_group(1, 1, FRIDGE, "none"))
    assert result == {"product_id": 1, "consumed": 1, "requested": 1, "complete": True}
    assert grocy.at(FRIDGE) == [(_d(9), 2)]


def test_a_row_that_is_gone_answers_gone_and_writes_nothing(grocy):
    grocy.add(2, _d(3))
    grocy.add(DUST, _d(9))
    refused = _refused(GrocyClient().consume_stock_group(1, 1, FRIDGE, _d(9)))
    assert (refused.kind, str(refused)) == ("gone", BEST_BY_ROW_GONE)
    assert grocy.writes() == []


def test_a_product_weighed_with_its_container_is_refused(grocy):
    grocy.product["enable_tare_weight_handling"] = 1
    grocy.add(400, _d(9))
    for call in (GrocyClient().consume_stock_group(1, 1, FRIDGE, _d(9)),
                 GrocyClient().open_stock_group(1, FRIDGE, _d(9)),
                 GrocyClient().move_product(1, "frozen", from_location_id=FRIDGE,
                                            from_best_before_date=_d(9))):
        refused = _refused(call)
        assert (refused.kind, str(refused)) == ("group", BEST_BY_ROW_TARE)
    assert grocy.writes() == []


def test_a_use_stopped_part_way_says_how_much_it_used(grocy):
    """Another device puts an earlier unit carrying the second entry's stock
    id in the fridge after the first call: the rest is out of reach."""
    first = grocy.add(1, _d(9), price=1.0)
    second = grocy.add(2, _d(9))
    arrived = []

    def another_device(method, path, body):
        if not arrived:
            arrived.append(grocy.add(1, _d(2), stock_id=second["stock_id"]))

    grocy.after_write = another_device
    result = _run(GrocyClient().consume_stock_group(1, 3, FRIDGE, _d(9)))
    assert [c["stock_entry_id"] for c in grocy.sent("POST", "/consume")] == [first["stock_id"]]
    assert result == {"product_id": 1, "consumed": 1, "requested": 3, "complete": False}
    assert grocy.at(FRIDGE) == [(_d(2), 1), (_d(9), 2)]


def test_a_stock_id_renamed_before_the_write_is_read_again(grocy):
    """A merge elsewhere renamed the row's stock id after the read. Grocy books
    nothing for the old id, so the row is read again and taken by the new one."""
    row = grocy.add(2, _d(9))
    read = row["stock_id"]

    def merge_elsewhere(method, path, body):
        if row["stock_id"] == body.get("stock_entry_id"):
            row["stock_id"] = "6ab9690b00001"

    grocy.before_write = merge_elsewhere
    result = _run(GrocyClient().consume_stock_group(1, 2, FRIDGE, _d(9)))
    assert [c["stock_entry_id"] for c in grocy.sent("POST", "/consume")] == [
        read, "6ab9690b00001"]
    assert result == {"product_id": 1, "consumed": 2, "requested": 2, "complete": True}
    assert grocy.at(FRIDGE) == []


def test_a_booking_outside_the_row_stops_and_is_not_counted(grocy):
    """Another device used one unit of the row between the read and the call,
    so Grocy took the rest from the next row with the stock id, a later date.
    Nothing more is taken after that, not even the row's other entry."""
    row = grocy.add(2, _d(4))
    later = grocy.add(1, _d(9), stock_id=row["stock_id"], price=1.0)
    other = grocy.add(3, _d(4), price=2.0)
    used = []

    def another_device(method, path, body):
        if not used:
            used.append(1)
            row["amount"] = 1

    grocy.before_write = another_device
    result = _run(GrocyClient().consume_stock_group(1, 5, FRIDGE, _d(4)))
    assert result == {"product_id": 1, "consumed": 1, "requested": 5, "complete": False}
    assert later["id"] not in grocy.rows
    assert len(grocy.sent("POST", "/consume")) == 1
    assert other["amount"] == 3


# --- Mark one opened ------------------------------------------------------------

def test_mark_one_opened_opens_a_unit_of_the_rows_own_entry(grocy):
    earlier = grocy.add(2, _d(2))       # what a plain open takes
    row = grocy.add(3, _d(30))
    result = _run(GrocyClient().open_stock_group(1, FRIDGE, _d(30)))
    assert grocy.sent("POST", "/open") == [{"amount": 1, "stock_entry_id": row["stock_id"]}]
    assert result == {"product_id": 1, "opened": 1, "new_best_by": _d(3), "complete": True}
    # The opened unit keeps the row; the unopened rest is a new row with a new
    # stock id, and the earlier date is untouched.
    assert (row["amount"], row["open"], row["best_before_date"]) == (1, 1, _d(3))
    (rest,) = [r for r in grocy.rows.values() if r["id"] not in (earlier["id"], row["id"])]
    assert (rest["amount"], rest["open"], rest["best_before_date"]) == (2, 0, _d(30))
    assert rest["stock_id"] != row["stock_id"]
    assert (earlier["amount"], earlier["open"]) == (2, 0)


def test_opening_half_a_unit_stops_at_the_rows_own_half(grocy):
    """Probe o: asked for 1 on a 0.5 row, Grocy opened the half and then half
    a unit in another place with the same stock id."""
    half = grocy.add(0.5, _d(4))
    elsewhere = grocy.add(2, _d(4), CELLAR, stock_id=half["stock_id"])
    result = _run(GrocyClient().open_stock_group(1, FRIDGE, _d(4)))
    assert grocy.sent("POST", "/open") == [{"amount": 0.5, "stock_entry_id": half["stock_id"]}]
    assert result["opened"] == 0.5 and result["complete"] is True
    assert (elsewhere["amount"], elsewhere["open"]) == (2, 0)


def test_an_open_grocy_would_do_in_another_place_writes_nothing(grocy):
    grocy.product["default_consume_location_id"] = FREEZER
    row = grocy.add(2, _d(4))
    grocy.add(1, _d(60), FREEZER, stock_id=row["stock_id"])
    refused = _refused(GrocyClient().open_stock_group(1, FRIDGE, _d(4)))
    assert (refused.kind, str(refused)) == (
        "group", "Grocy would open one in Freezer first, so nothing was opened.")
    assert grocy.writes() == []


def test_an_open_grocy_would_do_on_another_date_writes_nothing(grocy):
    earlier = grocy.add(1, _d(2))
    grocy.add(2, _d(9), stock_id=earlier["stock_id"], price=2.0)
    refused = _refused(GrocyClient().open_stock_group(1, FRIDGE, _d(9)))
    assert str(refused) == ("Grocy would open one with another best-by date first, "
                            "so nothing was opened.")
    assert grocy.writes() == []


def test_a_row_already_open_says_so(grocy):
    grocy.add(1, _d(3), opened=True)
    grocy.add(2, _d(9))
    refused = _refused(GrocyClient().open_stock_group(1, FRIDGE, _d(3)))
    assert (refused.kind, str(refused)) == ("group", "Everything on this row is already open.")
    gone = _refused(GrocyClient().open_stock_group(1, FREEZER, _d(3)))
    assert (gone.kind, str(gone)) == ("gone", BEST_BY_ROW_GONE)
    assert grocy.writes() == []


def test_an_opened_twin_does_not_block_opening_the_rest(grocy):
    twin = grocy.add(1, _d(3), opened=True)
    row = grocy.add(5, _d(9), stock_id=twin["stock_id"])
    result = _run(GrocyClient().open_stock_group(1, FRIDGE, _d(9)))
    assert result["opened"] == 1 and result["complete"] is True
    assert (row["amount"], row["open"]) == (1, 1)


def test_an_open_that_moves_the_unit_counts_only_the_open(grocy):
    """With move_on_open, Grocy's answer also carries the transfer of the
    opened unit to the default consume location."""
    grocy.product.update(move_on_open=1, default_consume_location_id=CELLAR)
    row = grocy.add(2, _d(9))
    result = _run(GrocyClient().open_stock_group(1, FRIDGE, _d(9)))
    assert {r["transaction_type"] for r in grocy.log} == {
        "product-opened", "transfer_from", "transfer_to"}
    assert result == {"product_id": 1, "opened": 1, "new_best_by": _d(3), "complete": True}
    assert (row["location_id"], row["open"]) == (CELLAR, 1)


def test_opening_undated_stock_keeps_it_undated(grocy):
    grocy.add(2, None)
    result = _run(GrocyClient().open_stock_group(1, FRIDGE, "none"))
    assert result == {"product_id": 1, "opened": 1, "new_best_by": None, "complete": True}


# --- Edit -------------------------------------------------------------------------

def test_a_per_date_edit_moves_only_that_rows_dates(grocy):
    grocy.add(2, _d(3))
    grocy.add(3, _d(30))
    grocy.add(1, _d(30), FREEZER)
    result = _run(GrocyClient().edit_product(
        1, best_before_date=_d(40), from_location_id=FRIDGE, from_best_before_date=_d(30)))
    assert result == {"product_id": 1, "updated": 1}
    assert grocy.at(FRIDGE) == [(_d(3), 2), (_d(40), 3)]
    assert grocy.at(FREEZER) == [(_d(30), 1)]
    assert len(grocy.entry_writes()) == 1


def test_a_per_date_edit_onto_another_date_merges_without_losing_units(grocy):
    """Both of the row's entries land on a date another entry there already
    has with the same details, so each write merges, and every unit stays."""
    grocy.add(2, _d(3), note="Fage")
    grocy.add(1, _d(9), note="Fage")
    grocy.add(4, _d(9), note="Fage")
    result = _run(GrocyClient().edit_product(
        1, best_before_date=_d(3), from_location_id=FRIDGE, from_best_before_date=_d(9)))
    assert result == {"product_id": 1, "updated": 2}
    assert grocy.at(FRIDGE) == [(_d(3), 7)]
    assert grocy.total() == 7


def test_a_per_date_edit_of_undated_stock(grocy):
    grocy.add(2, None)
    grocy.add(1, _d(5))
    result = _run(GrocyClient().edit_product(
        1, best_before_date=_d(8), from_location_id=FRIDGE, from_best_before_date="none"))
    assert result == {"product_id": 1, "updated": 1}
    assert grocy.at(FRIDGE) == [(_d(5), 1), (_d(8), 2)]


def test_an_entry_that_left_the_row_since_the_read_keeps_its_date(grocy):
    first = grocy.add(1, _d(9), price=1.0)
    second = grocy.add(2, _d(9))

    def another_device(method, path, body):
        second["location_id"] = FREEZER

    grocy.before_write = another_device
    result = _run(GrocyClient().edit_product(
        1, best_before_date=_d(12), from_location_id=FRIDGE, from_best_before_date=_d(9)))
    assert result == {"product_id": 1, "updated": 1}
    assert (first["best_before_date"], second["best_before_date"]) == (_d(12), _d(9))


def test_a_per_date_edit_of_a_row_that_is_gone_changes_nothing(grocy, monkeypatch):
    async def group_id(self, name):
        return 7

    monkeypatch.setattr(GrocyClient, "ensure_product_group", group_id)
    grocy.add(2, _d(3))
    refused = _refused(GrocyClient().edit_product(
        1, category="Snacks", best_before_date=_d(5),
        from_location_id=FRIDGE, from_best_before_date=_d(9)))
    assert (refused.kind, str(refused)) == ("gone", BEST_BY_ROW_GONE)
    # Not even the category: the row the edit came from is no longer there.
    assert grocy.writes() == []


def test_a_category_from_a_per_date_row_is_the_whole_products(grocy, monkeypatch):
    async def group_id(self, name):
        return 7

    monkeypatch.setattr(GrocyClient, "ensure_product_group", group_id)
    grocy.add(2, _d(9))
    result = _run(GrocyClient().edit_product(
        1, category="Snacks", from_location_id=FRIDGE, from_best_before_date=_d(9)))
    assert result == {"product_id": 1, "updated": 0}
    assert grocy.sent("PUT", "/objects/products/1") == [{"product_group_id": 7}]
    assert grocy.entry_writes() == []


def test_half_a_scope_never_dates_every_entry(grocy):
    grocy.add(2, _d(3))
    grocy.add(3, _d(9))
    for scope in ({"from_location_id": FRIDGE}, {"from_best_before_date": _d(9)}):
        _refused(GrocyClient().edit_product(1, best_before_date=_d(5), **scope))
    assert grocy.writes() == []


# --- Move -------------------------------------------------------------------------

def _move(**kwargs):
    return _run(GrocyClient().move_product(1, kwargs.pop("bucket", "frozen"), **kwargs))


def test_a_per_date_move_takes_only_that_rows_entries(grocy):
    grocy.add(2, _d(3))
    row = grocy.add(3, _d(30))
    result = _move(from_location_id=FRIDGE, from_best_before_date=_d(30))
    assert grocy.sent("POST", "/transfer") == [
        {"amount": 3, "location_id_from": FRIDGE, "location_id_to": FREEZER,
         "stock_entry_id": row["stock_id"]}]
    assert grocy.at(FRIDGE) == [(_d(3), 2)]
    assert grocy.at(FREEZER) == [(_d(30), 3)]
    assert result == {"product_id": 1, "moved_amount": 3, "location_id": FREEZER,
                      "best_by_updates": [], "new_best_by": None, "best_by_change": None,
                      "left_behind": 0}
    # Stock stays in the fridge, so the product keeps its default location.
    assert grocy.sent("PUT", "/objects/products/1") == []


def test_a_per_date_move_re_dates_the_arrival_right_after_the_transfer(grocy):
    grocy.add(2, _d(3))
    row = grocy.add(3, _d(30))
    asked = []

    def proposer(old, from_bucket):
        asked.append((old, from_bucket))
        return plus_90(old, from_bucket)

    result = _move(propose_best_by=proposer, from_location_id=FRIDGE,
                   from_best_before_date=_d(30))
    # The date comes once from the row's date and the place it leaves.
    assert asked == [(date.fromisoformat(_d(30)), "refrigerated")]
    assert grocy.writes() == [("POST", "/stock/products/1/transfer"),
                              ("PUT", f"/stock/entry/{row['id']}")]
    assert grocy.at(FREEZER) == [(_d(120), 3)]
    assert result["best_by_updates"] == [{"old": _d(30), "new": _d(120)}]
    assert (result["new_best_by"], result["best_by_change"]) == (_d(120), "extended")


def test_grocys_own_freezer_date_on_arrival_stands(grocy):
    grocy.product["default_best_before_days_after_freezing"] = 60
    grocy.add(3, _d(30))
    result = _move(propose_best_by=plus_90, from_location_id=FRIDGE,
                   from_best_before_date=_d(30))
    assert grocy.entry_writes() == []
    assert grocy.at(FREEZER) == [(_d(60), 3)]
    assert result["best_by_updates"] == [{"old": _d(30), "new": _d(60)}]
    assert (result["new_best_by"], result["best_by_change"]) == (_d(60), "extended")


def test_a_whole_move_reports_grocys_own_freezer_date(grocy):
    """The whole-product Move dates each row before its transfer, and Grocy
    then dates it again on the way into its freezer: the answer (and the
    toast) carries the date Grocy holds."""
    grocy.product["default_best_before_days_after_freezing"] = 60
    grocy.add(3, _d(30))
    grocy.add(1, _d(4))
    result = _move(propose_best_by=plus_90)
    assert grocy.at(FREEZER) == [(_d(60), 1), (_d(60), 3)]
    assert sorted(u["new"] for u in result["best_by_updates"]) == [_d(60), _d(60)]
    assert sorted(u["old"] for u in result["best_by_updates"]) == [_d(4), _d(30)]
    assert (result["new_best_by"], result["best_by_change"]) == (_d(60), "extended")


def test_a_whole_move_thawing_with_grocys_days_reports_them(grocy):
    grocy.product["default_best_before_days_after_thawing"] = 2
    grocy.add(1, _d(200), FREEZER)
    result = _move(bucket="refrigerated")
    assert grocy.at(FRIDGE) == [(_d(2), 1)]
    assert result["best_by_updates"] == [{"old": _d(200), "new": _d(2)}]
    assert (result["new_best_by"], result["best_by_change"]) == (_d(2), "shortened")


def test_a_move_grocy_would_mix_with_other_stock_writes_nothing(grocy):
    """Checked before anything is written, even the new place: the pantry
    location does not exist yet in this Grocy."""
    earlier = grocy.add(1, _d(2))
    grocy.add(2, _d(9), stock_id=earlier["stock_id"], price=1.0)
    refused = _refused(GrocyClient().move_product(
        1, "pantry", from_location_id=FRIDGE, from_best_before_date=_d(9)))
    assert (refused.kind, str(refused)) == (
        "group", "Grocy would move other stock with it, so nothing was moved.")
    gone = _refused(GrocyClient().move_product(
        1, "pantry", from_location_id=FREEZER, from_best_before_date=_d(9)))
    assert (gone.kind, str(gone)) == ("gone", BEST_BY_ROW_GONE)
    assert grocy.writes() == []


def test_a_move_whose_plan_a_merge_broke_says_what_it_left_behind(grocy):
    """Probe m: re-dating the first arrival merged it with a row already in the
    freezer, and the merge kept that row's smaller stock id, renaming the
    row's last entry in the fridge to the stock id of an earlier date there."""
    earlier = grocy.add(1, _d(2))
    grocy.add(1, _d(4), stock_id="6ab9690b00000")
    grocy.add(1, _d(4), stock_id="6ab9690b00000")
    grocy.add(1, _d(94), FREEZER, stock_id=earlier["stock_id"])
    result = _move(propose_best_by=plus_90, from_location_id=FRIDGE,
                   from_best_before_date=_d(4))
    assert len(grocy.sent("POST", "/transfer")) == 1
    assert grocy.at(FRIDGE) == [(_d(2), 1), (_d(4), 1)]
    assert grocy.at(FREEZER) == [(_d(94), 2)]
    assert result["moved_amount"] == 1 and result["left_behind"] == 1
    assert result["best_by_updates"] == [{"old": _d(4), "new": _d(94)}]
    assert grocy.sent("PUT", "/objects/products/1") == []


def test_a_move_stops_as_soon_as_part_of_the_row_is_out_of_reach(grocy):
    """The same merge with a third entry of its own stock id: that entry
    could still move, but the rest of the row could not follow it, so the
    move stops rather than split the row further."""
    earlier = grocy.add(1, _d(2))
    grocy.add(1, _d(4), stock_id="6ab9690b00000")
    grocy.add(1, _d(4), stock_id="6ab9690b00000")
    grocy.add(1, _d(4), stock_id="6ab9690c00000", price=1.0)
    grocy.add(1, _d(94), FREEZER, stock_id=earlier["stock_id"])
    result = _move(propose_best_by=plus_90, from_location_id=FRIDGE,
                   from_best_before_date=_d(4))
    assert len(grocy.sent("POST", "/transfer")) == 1
    assert grocy.at(FRIDGE) == [(_d(2), 1), (_d(4), 1), (_d(4), 1)]
    assert result["moved_amount"] == 1 and result["left_behind"] == 2


def test_a_per_date_move_into_its_own_place_writes_nothing(grocy):
    grocy.add(2, _d(9), FREEZER)
    result = _move(propose_best_by=plus_90, from_location_id=FREEZER,
                   from_best_before_date=_d(9))
    assert grocy.writes() == []
    assert result["moved_amount"] == 0 and result["left_behind"] == 0


def test_each_arrival_is_re_dated_before_the_next_transfer(grocy):
    """Probe y: with every transfer done first, the first date write merged a
    moved entry with freezer stock of the same date that was not moving, and
    the second write then re-dated that stock too."""
    staying = grocy.add(1, _d(4), FREEZER)
    first = grocy.add(1, _d(4), price=1.0)
    second = grocy.add(1, _d(4))
    result = _move(propose_best_by=plus_90, from_location_id=FRIDGE,
                   from_best_before_date=_d(4))
    assert grocy.writes() == [
        ("POST", "/stock/products/1/transfer"), ("PUT", f"/stock/entry/{first['id']}"),
        ("POST", "/stock/products/1/transfer"), ("PUT", f"/stock/entry/{second['id']}"),
        ("PUT", "/objects/products/1")]
    assert (staying["amount"], staying["best_before_date"]) == (1, _d(4))
    assert grocy.at(FREEZER) == [(_d(4), 1), (_d(94), 1), (_d(94), 1)]
    assert result["moved_amount"] == 2 and result["left_behind"] == 0
    # Nothing of the product is left elsewhere, so the default follows.
    assert grocy.sent("PUT", "/objects/products/1") == [{"location_id": FREEZER}]


def test_undated_stock_moves_without_a_new_date(grocy):
    grocy.add(2, None)
    grocy.add(1, _d(3))
    result = _move(propose_best_by=plus_90, from_location_id=FRIDGE,
                   from_best_before_date="none")
    assert grocy.entry_writes() == []
    assert grocy.at(FREEZER) == [(None, 2)]
    assert result["moved_amount"] == 2 and result["best_by_updates"] == []


def test_a_transfer_that_took_other_stock_too_stops_the_move(grocy):
    """Another device used half of the first entry between the read and the
    transfer, so Grocy moved the rest from a later date with its stock id."""
    first = grocy.add(1, _d(4))
    later = grocy.add(1, _d(9), stock_id=first["stock_id"], price=1.0)
    grocy.add(2, _d(4), price=2.0)
    used = []

    def another_device(method, path, body):
        if not used:
            used.append(1)
            first["amount"] = 0.5

    grocy.before_write = another_device
    result = _move(from_location_id=FRIDGE, from_best_before_date=_d(4))
    assert len(grocy.sent("POST", "/transfer")) == 1
    assert later["amount"] == 0.5
    assert result["moved_amount"] == 0.5 and result["left_behind"] == 2


def test_a_move_reads_again_once_when_a_merge_renamed_the_stock_id(grocy):
    row = grocy.add(3, _d(9))
    read = row["stock_id"]

    def merge_elsewhere(method, path, body):
        if row["stock_id"] == body.get("stock_entry_id"):
            row["stock_id"] = "6ab9690b00001"

    grocy.before_write = merge_elsewhere
    result = _move(from_location_id=FRIDGE, from_best_before_date=_d(9))
    assert [t["stock_entry_id"] for t in grocy.sent("POST", "/transfer")] == [
        read, "6ab9690b00001"]
    assert grocy.at(FREEZER) == [(_d(9), 3)]
    assert result["moved_amount"] == 3 and result["left_behind"] == 0


# --- Move part of a row --------------------------------------------------------------

def test_a_per_date_move_of_part_splits_the_row_and_re_dates_only_the_arrival(grocy):
    """Freezing 4 of 6: the 2 left in the fridge keep their date."""
    row = grocy.add(6, _d(5))
    result = _move(propose_best_by=plus_90, from_location_id=FRIDGE,
                   from_best_before_date=_d(5), amount=4)
    assert grocy.sent("POST", "/transfer") == [
        {"amount": 4, "location_id_from": FRIDGE, "location_id_to": FREEZER,
         "stock_entry_id": row["stock_id"]}]
    assert grocy.at(FRIDGE) == [(_d(5), 2)]
    assert grocy.at(FREEZER) == [(_d(95), 4)]
    assert result["moved_amount"] == 4 and result["left_behind"] == 0
    assert result["best_by_updates"] == [{"old": _d(5), "new": _d(95)}]
    # Stock stays in the fridge, so the product keeps its default location.
    assert grocy.sent("PUT", "/objects/products/1") == []


def test_a_move_of_part_takes_whole_entries_before_splitting_one(grocy):
    grocy.add(2, _d(5))
    grocy.add(3, _d(5), price=1.0)
    result = _move(from_location_id=FRIDGE, from_best_before_date=_d(5), amount=3)
    assert [t["amount"] for t in grocy.sent("POST", "/transfer")] == [2, 1]
    assert grocy.at(FRIDGE) == [(_d(5), 2)]
    assert grocy.at(FREEZER) == [(_d(5), 1), (_d(5), 2)]
    assert result["moved_amount"] == 3 and result["left_behind"] == 0


def test_asking_to_move_at_least_the_row_moves_all_of_it(grocy):
    grocy.add(0.3, _d(5))
    result = _move(from_location_id=FRIDGE, from_best_before_date=_d(5), amount=7)
    assert [t["amount"] for t in grocy.sent("POST", "/transfer")] == [0.3]
    assert grocy.at(FRIDGE) == []
    assert result["moved_amount"] == 0.3 and result["left_behind"] == 0


def test_a_move_of_part_reachable_without_other_stock_is_not_refused(grocy):
    """The later entry sits behind an earlier date with its stock id, but the
    first one alone covers the amount asked for."""
    earlier = grocy.add(1, _d(2))
    grocy.add(2, _d(9), price=1.0)
    grocy.add(1, _d(9), stock_id=earlier["stock_id"], price=2.0)
    result = _move(from_location_id=FRIDGE, from_best_before_date=_d(9), amount=2)
    assert result["moved_amount"] == 2 and result["left_behind"] == 0
    assert grocy.at(FRIDGE) == [(_d(2), 1), (_d(9), 1)]
    refused = _refused(GrocyClient().move_product(
        1, "frozen", from_location_id=FRIDGE, from_best_before_date=_d(9), amount=1))
    assert refused.kind == "group"


def test_a_move_of_part_of_a_place_goes_date_by_date_in_grocys_order(grocy):
    """A row of one place with two dates: the stock Grocy would use first
    moves first, and each date is re-dated on its own."""
    grocy.add(3, _d(9))
    grocy.add(2, _d(4), price=1.0)
    result = _move(propose_best_by=plus_90, from_location_id=FRIDGE, amount=3)
    assert [t["amount"] for t in grocy.sent("POST", "/transfer")] == [2, 1]
    assert grocy.at(FRIDGE) == [(_d(9), 2)]
    assert grocy.at(FREEZER) == [(_d(94), 2), (_d(99), 1)]
    assert result["moved_amount"] == 3 and result["left_behind"] == 0
    assert (result["new_best_by"], result["best_by_change"]) == (_d(94), "extended")
    assert grocy.sent("PUT", "/objects/products/1") == []


def test_a_move_of_all_of_a_place_by_amount_is_the_whole_place_move(grocy):
    grocy.add(2, _d(4))
    grocy.add(1, _d(9), price=1.0)
    result = _move(from_location_id=FRIDGE, amount=3)
    assert grocy.at(FRIDGE) == []
    assert result["moved_amount"] == 3 and "left_behind" not in result
    assert grocy.sent("PUT", "/objects/products/1") == [{"location_id": FREEZER}]


def test_a_move_of_part_needs_its_place_and_refuses_tare_weight(grocy):
    grocy.add(6, _d(5))
    assert "place" in str(_refused(GrocyClient().move_product(1, "frozen", amount=2)))
    grocy.product["enable_tare_weight_handling"] = "1"
    refused = _refused(GrocyClient().move_product(
        1, "frozen", from_location_id=FRIDGE, amount=2))
    assert (refused.kind, str(refused)) == ("group", BEST_BY_ROW_TARE)
    assert grocy.writes() == []


@pytest.mark.parametrize("first,second,want", [(0.3, 0.4, 0.3), (0.3, 0.4, 0.1), (0.2, 1.1, 0.1)])
def test_a_finished_move_of_part_of_a_row_leaves_nothing_behind(grocy, first, second, want):
    """What the sums leave of a finished decimal move is float dust, not stock
    left behind, which the page would warn about."""
    grocy.add(first, _d(5))
    grocy.add(second, _d(5), price=1.0)
    result = _move(from_location_id=FRIDGE, from_best_before_date=_d(5), amount=want)
    assert result["moved_amount"] == pytest.approx(want)
    assert result["left_behind"] == 0


@pytest.mark.parametrize("first,second,want", [(0.1, 0.4, 0.45), (0.3, 0.3, 0.45), (0.6, 1.3, 0.9)])
def test_a_finished_move_of_part_of_a_place_leaves_nothing_behind(grocy, first, second, want):
    grocy.add(first, _d(5))
    grocy.add(second, _d(9), price=1.0)
    result = _move(from_location_id=FRIDGE, amount=want)
    assert result["moved_amount"] == pytest.approx(want)
    assert result["left_behind"] == 0


# --- What the page showed, and the place-wide toss --------------------------------

def test_a_row_that_still_holds_what_the_page_showed_is_used(grocy):
    grocy.add(0.1, _d(9))
    grocy.add(0.2, _d(9), price=1.0)
    result = _run(GrocyClient().consume_stock_group(
        1, 0.3, FRIDGE, _d(9), spoiled=True, expected_amount=0.1 + 0.2))
    assert result["complete"] is True
    assert grocy.at(FRIDGE) == []


def test_a_row_that_changed_since_the_page_loaded_writes_nothing(grocy):
    grocy.add(2, _d(9))                 # the page showed 3; someone used one
    refused = _refused(GrocyClient().consume_stock_group(
        1, 3, FRIDGE, _d(9), spoiled=True, expected_amount=3))
    assert refused.kind == "changed"
    assert str(refused) == ("This row changed since the page loaded: it now holds 2, "
                            "not 3. Nothing was changed.")
    assert grocy.writes() == []


def test_a_row_emptied_since_the_page_loaded_is_gone(grocy):
    grocy.add(2, _d(3))
    refused = _refused(GrocyClient().consume_stock_group(
        1, 1, FRIDGE, _d(9), expected_amount=1))
    assert (refused.kind, str(refused)) == ("gone", BEST_BY_ROW_GONE)


def test_an_opened_twin_refusal_says_the_place_wide_toss_can_help(grocy):
    opened = grocy.add(1, _d(20), opened=True)
    grocy.add(3, _d(9), stock_id=opened["stock_id"])
    refused = _refused(GrocyClient().consume_stock_group(1, 3, FRIDGE, _d(9), spoiled=True))
    assert refused.opened_first is True


def test_an_earlier_date_refusal_offers_no_place_wide_toss(grocy):
    earlier = grocy.add(1, _d(2))
    grocy.add(2, _d(12), stock_id=earlier["stock_id"])
    refused = _refused(GrocyClient().consume_stock_group(1, 2, FRIDGE, _d(12)))
    assert refused.opened_first is False


def test_the_place_wide_toss_takes_the_rows_amount_in_grocys_order(grocy):
    opened = grocy.add(1, _d(20), opened=True)
    grocy.add(3, _d(9), stock_id=opened["stock_id"])
    grocy.add(5, _d(9), FREEZER)
    result = _run(GrocyClient().consume_stock_at_place(
        1, 3, FRIDGE, _d(9), spoiled=True, expected_amount=3))
    assert grocy.sent("POST", "/consume") == [
        {"amount": 3, "spoiled": True, "location_id": FRIDGE}]
    assert result["complete"] is True and result["place_wide"] is True
    # The opened one went first, as the question said; the freezer is untouched.
    assert grocy.at(FRIDGE) == [(_d(9), 1)]
    assert grocy.at(FREEZER) == [(_d(9), 5)]


def test_the_place_wide_toss_still_checks_what_the_page_showed(grocy):
    opened = grocy.add(1, _d(20), opened=True)
    grocy.add(2, _d(9), stock_id=opened["stock_id"])
    refused = _refused(GrocyClient().consume_stock_at_place(
        1, 3, FRIDGE, _d(9), spoiled=True, expected_amount=3))
    assert refused.kind == "changed"
    assert grocy.writes() == []


def test_the_place_wide_toss_refuses_tare_weight(grocy):
    grocy.product["enable_tare_weight_handling"] = 1
    grocy.add(400, _d(9))
    refused = _refused(GrocyClient().consume_stock_at_place(1, 400, FRIDGE, _d(9)))
    assert (refused.kind, str(refused)) == ("group", BEST_BY_ROW_TARE)
    assert grocy.writes() == []
