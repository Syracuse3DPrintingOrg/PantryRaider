"""Best-by follows storage transfers (FoodAssistant-jty6).

When stock moves between locations with different natures, the best-by date
should update intuitively: freezing extends (recomputed from today, never
shortening a later date), thawing shortens (recomputed from today, never
extending, since Grocy keeps no record of the pre-freeze date). Covers the
pure proposal matrix, the rule lookup (user rule vs community override vs
built-in vs category fallback), and the hook on the transfer path.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent / "service"))

from app.database import Base  # noqa: E402
from app.models import db_models  # noqa: E402,F401 - registers tables with Base
from app.models.db_models import ExpiryDefault  # noqa: E402
from app.services import defaults  # noqa: E402
from app.services.defaults import (  # noqa: E402
    propose_transfer_best_by, resolve_rule_days, seed_defaults,
    storage_kind_for_bucket,
)
from app.services.grocy import GrocyClient  # noqa: E402

TODAY = date(2026, 7, 15)


def _seeded_db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    seed_defaults(db)
    return db


# --- Bucket -> storage kind ----------------------------------------------------

def test_bucket_kind_mapping():
    assert storage_kind_for_bucket("refrigerated") == "refrigerated"
    assert storage_kind_for_bucket("frozen") == "frozen"
    assert storage_kind_for_bucket("room_temp") == "room_temp"
    assert storage_kind_for_bucket("pantry") == "dry"
    # Custom buckets and "other" have no knowable temperature.
    assert storage_kind_for_bucket("other") is None
    assert storage_kind_for_bucket("wine_cellar") is None
    assert storage_kind_for_bucket(None) is None


# --- The proposal matrix (pure) --------------------------------------------------

def _in(days: int) -> date:
    return TODAY + timedelta(days=days)


@pytest.mark.parametrize("from_kind,to_kind,old,dest_days,expected", [
    # Freezing extends: recompute from today with the freezer rule.
    ("refrigerated", "frozen", _in(3), 120, _in(120)),
    ("dry", "frozen", _in(10), 365, _in(365)),
    ("room_temp", "frozen", _in(2), 240, _in(240)),
    # ... and never shortens a later date already on the item.
    ("refrigerated", "frozen", _in(500), 120, None),
    # Thawing shortens: recompute from today with the fridge rule.
    ("frozen", "refrigerated", _in(300), 5, _in(5)),
    ("frozen", "room_temp", _in(300), 2, _in(2)),
    ("frozen", "dry", _in(300), 30, _in(30)),
    # ... and never extends: the pre-freeze horizon is not stored by Grocy,
    # so the honest cap is the date currently on the item.
    ("frozen", "refrigerated", _in(2), 14, None),
    # Fridge <-> ambient behaves the same way by temperature direction.
    ("room_temp", "refrigerated", _in(2), 14, _in(14)),   # colder: extend
    ("dry", "refrigerated", _in(2), 14, _in(14)),
    ("refrigerated", "room_temp", _in(20), 5, _in(5)),    # warmer: shorten
    ("refrigerated", "dry", _in(20), 5, _in(5)),
    # Two ambient shelves are the same temperature: a shelf swap, no change.
    ("dry", "room_temp", _in(20), 5, None),
    ("room_temp", "dry", _in(3), 700, None),
    # Same kind: no change.
    ("refrigerated", "refrigerated", _in(3), 14, None),
    # Unknown kinds (custom buckets, "other") propose nothing.
    (None, "frozen", _in(3), 120, None),
    ("refrigerated", None, _in(3), 120, None),
    ("garage", "frozen", _in(3), 120, None),
])
def test_proposal_matrix(from_kind, to_kind, old, dest_days, expected):
    assert propose_transfer_best_by(old, from_kind, to_kind, dest_days, TODAY) == expected


def test_missing_rule_or_missing_date_proposes_nothing():
    assert propose_transfer_best_by(_in(3), "refrigerated", "frozen", None, TODAY) is None
    assert propose_transfer_best_by(None, "refrigerated", "frozen", 120, TODAY) is None


def test_no_change_when_recompute_lands_on_the_same_date():
    # Recomputing to exactly the current date is not a change.
    assert propose_transfer_best_by(_in(5), "frozen", "refrigerated", 5, TODAY) is None


# --- Destination rule lookup ------------------------------------------------------

def test_lookup_uses_the_seed_rule_for_the_destination(monkeypatch):
    db = _seeded_db()
    monkeypatch.setattr(defaults, "date", date)  # no-op; keeps import honest
    assert resolve_rule_days(db, "Chicken thighs", "Poultry", "frozen") == 365
    assert resolve_rule_days(db, "Chicken thighs", "Poultry", "refrigerated") == 5
    assert resolve_rule_days(db, "Broccoli crowns", "Produce", "refrigerated") == 7


def test_lookup_falls_back_to_the_category_default():
    db = _seeded_db()
    # No name rule matches, so the generic Produce/frozen fallback applies.
    assert resolve_rule_days(db, "Dragonfruit", "Produce", "frozen") == 365
    # Unknown category falls back to Other; unknown storage kind is honest None.
    assert resolve_rule_days(db, "Mystery jar", "Homemade", "refrigerated") == 7
    assert resolve_rule_days(db, "Mystery jar", "Produce", "wine_cellar") is None


def test_users_own_rule_beats_a_community_override(monkeypatch):
    db = _seeded_db()
    row = (db.query(ExpiryDefault)
           .filter_by(name_pattern="broccoli", storage_type="refrigerated").one())
    row.default_days = 9  # the user edited the seed rule: it is theirs now
    db.commit()
    monkeypatch.setattr("app.services.community_expiry.suggested_days",
                        lambda name, barcode, storage: 4)
    assert resolve_rule_days(db, "Broccoli", "Produce", "refrigerated") == 9


def test_community_override_beats_the_builtin_rule(monkeypatch):
    db = _seeded_db()
    monkeypatch.setattr("app.services.community_expiry.suggested_days",
                        lambda name, barcode, storage: 11)
    assert resolve_rule_days(db, "Broccoli", "Produce", "refrigerated") == 11


# --- The hook on the transfer path -------------------------------------------------

def _fake_move_client(monkeypatch, entries, locations):
    calls = []

    async def fake_get(self, path):
        if path == f"/stock/products/1/entries":
            return entries
        if path == "/objects/locations":
            return locations
        return []

    async def fake_post(self, path, body):
        calls.append(("POST", path, body))
        return {"created_object_id": 99}

    async def fake_request(self, method, path, body=None):
        calls.append((method, path, body))
        return {}

    monkeypatch.setattr(GrocyClient, "_get", fake_get)
    monkeypatch.setattr(GrocyClient, "_cached_list", fake_get)
    monkeypatch.setattr(GrocyClient, "_post", fake_post)
    monkeypatch.setattr(GrocyClient, "_request", fake_request)
    return GrocyClient(), calls


LOCATIONS = [
    {"id": 2, "name": "Refrigerator"},
    {"id": 3, "name": "Freezer"},
]


def test_move_rewrites_the_date_before_the_transfer(monkeypatch):
    old = (date.today() + timedelta(days=3)).isoformat()
    new = date.today() + timedelta(days=120)
    c, calls = _fake_move_client(monkeypatch, [
        {"id": 51, "amount": 2, "location_id": 2, "best_before_date": old},
    ], LOCATIONS)

    def proposer(old_date, from_bucket):
        assert from_bucket == "refrigerated"  # classified from the location name
        return new

    result = asyncio.run(c.move_product(1, "frozen", propose_best_by=proposer))
    assert result["best_by_updates"] == [{"old": old, "new": new.isoformat()}]
    assert result["new_best_by"] == new.isoformat()
    assert result["best_by_change"] == "extended"
    ops = [(m, p) for m, p, _b in calls]
    # The date write lands before the transfer, so the moved stock carries it.
    assert ops.index(("PUT", "/objects/stock/51")) < ops.index(
        ("POST", "/stock/products/1/transfer"))


def test_move_reports_shortened_when_thawing(monkeypatch):
    old = (date.today() + timedelta(days=300)).isoformat()
    new = date.today() + timedelta(days=5)
    c, _calls = _fake_move_client(monkeypatch, [
        {"id": 51, "amount": 1, "location_id": 3, "best_before_date": old},
    ], LOCATIONS)
    result = asyncio.run(c.move_product(
        1, "refrigerated", propose_best_by=lambda o, b: new))
    assert result["best_by_change"] == "shortened"
    assert result["new_best_by"] == new.isoformat()


def test_move_leaves_dates_alone_when_nothing_is_proposed(monkeypatch):
    old = (date.today() + timedelta(days=3)).isoformat()
    c, calls = _fake_move_client(monkeypatch, [
        {"id": 51, "amount": 1, "location_id": 2, "best_before_date": old},
        {"id": 52, "amount": 1, "location_id": None, "best_before_date": old},
    ], LOCATIONS)
    result = asyncio.run(c.move_product(1, "frozen", propose_best_by=lambda o, b: None))
    assert result["best_by_updates"] == []
    assert result["new_best_by"] is None
    assert result["best_by_change"] is None
    assert not any(p.startswith("/objects/stock/") for _m, p, _b in calls)


def test_move_without_a_proposer_behaves_as_before(monkeypatch):
    old = (date.today() + timedelta(days=3)).isoformat()
    c, calls = _fake_move_client(monkeypatch, [
        {"id": 51, "amount": 2.0, "location_id": 2, "best_before_date": old},
    ], LOCATIONS)
    result = asyncio.run(c.move_product(1, "frozen"))
    assert result["moved_amount"] == 2.0
    assert result["best_by_updates"] == []
    assert not any(p.startswith("/objects/stock/") for _m, p, _b in calls)
