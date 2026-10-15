"""The sniff safety gate (FoodAssistant-uqci).

Grocy products carry a due type: 1 is a best-before (a quality guess, fair
game for the sniff test) and 2 is a hard expiration (a safety date). No sniff
surface may offer +1/+3/+5 on a hard-expiration item: not the Expiring page,
not the Review screen's chips. Consume and toss must remain.
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services.grocy import (  # noqa: E402
    GrocyClient, is_hard_expiry, stock_sniff_candidates,
)

TODAY = date(2026, 7, 15)
SOON = (TODAY + timedelta(days=2)).isoformat()


# --- is_hard_expiry truth table ----------------------------------------------

@pytest.mark.parametrize("entry,expected", [
    # due_type on the row itself
    ({"due_type": 2}, True),
    ({"due_type": 1}, False),
    ({"due_type": "2"}, True),               # Grocy string shape
    ({"due_type": "1"}, False),
    ({"due_type": None}, False),
    ({"due_type": "nonsense"}, False),       # unreadable counts as best-before
    # due_type on the embedded product
    ({"product": {"due_type": 2}}, True),
    ({"product": {"due_type": 1}}, False),
    ({"product": {"due_type": "2"}}, True),
    ({"product": {}}, False),
    # the row's own value wins over the product's
    ({"due_type": 1, "product": {"due_type": 2}}, False),
    ({"due_type": 2, "product": {"due_type": 1}}, True),
    # nothing anywhere
    ({}, False),
    (None, False),
])
def test_is_hard_expiry_truth_table(entry, expected):
    assert is_hard_expiry(entry) is expected


# --- stock_sniff_candidates gating -------------------------------------------

def _entry(name, pid, due_type=None, product_due=None):
    entry = {
        "product_id": pid,
        "amount": 1,
        "best_before_date": SOON,
        "product": {"name": name},
    }
    if due_type is not None:
        entry["due_type"] = due_type
    if product_due is not None:
        entry["product"]["due_type"] = product_due
    return entry


def test_hard_expiration_stock_never_becomes_a_sniff_candidate():
    stock = [
        _entry("Milk", 1, product_due=1),
        _entry("Deli Ham", 2, product_due=2),
        _entry("Eggs", 3, due_type=2),
        _entry("Yogurt", 4),  # no due type recorded: best-before by default
    ]
    out = stock_sniff_candidates(stock, today=TODAY)
    assert set(out) == {"milk", "yogurt"}


def test_a_best_before_duplicate_still_wins_over_a_gated_row():
    # Same product name twice: the hard-expiration row is skipped, the
    # best-before row still qualifies on its own merits.
    earlier = (TODAY + timedelta(days=1)).isoformat()
    stock = [
        {**_entry("Milk", 1, product_due=2), "best_before_date": earlier},
        _entry("Milk", 1, product_due=1),
    ]
    out = stock_sniff_candidates(stock, today=TODAY)
    assert out == {"milk": {"product_id": 1, "best_before_date": SOON,
                            "days_remaining": 2}}


# --- The Expiring page --------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    # An install with no inventory backend is "not set up", and the setup
    # redirect middleware answers every request with the wizard page, so the
    # endpoint under test never runs. Grocy itself is faked per test.
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "test-key", raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _page(client, items):
    async def fake_expiring(self, days=7):
        return items

    async def fake_get(self, path):
        return []

    async def fake_products(self):
        return []

    with patch.object(GrocyClient, "get_expiring", fake_expiring), \
         patch.object(GrocyClient, "_get", fake_get), \
         patch.object(GrocyClient, "get_products", fake_products), \
         patch.object(type(settings), "is_configured", lambda self: True):
        return client.get("/ui/expiring")


def test_hard_expiration_row_gets_no_sniff_chips_but_keeps_consume_and_toss(client):
    r = _page(client, [{
        "product_id": 5,
        "product": {"name": "Deli Ham", "due_type": 2},
        "amount": 1,
        "best_before_date": date.today().isoformat(),
        "days_remaining": 0,
    }])
    assert r.status_code == 200
    assert "sniffTest(5," not in r.text
    assert 'tossIt(5, &#34;Deli Ham&#34;, 1)' in r.text
    assert 'action="ui/consume/5"' in r.text
    # The row says why the chips are missing instead of leaving a silent gap.
    assert "Expiration date" in r.text


def test_best_before_row_still_gets_the_sniff_chips(client):
    r = _page(client, [{
        "product_id": 6,
        "product": {"name": "Broccoli", "due_type": 1},
        "amount": 1,
        "best_before_date": date.today().isoformat(),
        "days_remaining": 0,
    }])
    assert r.status_code == 200
    for delta in (1, 3, 5):
        assert f'sniffTest(6, &#34;Broccoli&#34;, {delta})' in r.text
