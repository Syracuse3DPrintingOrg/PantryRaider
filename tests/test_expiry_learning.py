"""Community shelf life (FoodAssistant-ezkh): capture, consent, and merge.

Covers the app side of expiry learning:

  * signal detection (expiry_learning.build_point): only a real user
    correction within the believable window becomes a data point, and the
    point carries exactly the anonymous fields and nothing else;
  * consent gating: with share_expiry_learning off (the default), record()
    captures NOTHING, not even a locally queued point, and a leftover queue
    is discarded rather than sent;
  * queue mechanics against a tmp data_dir: atomic file, cap, removal of
    uploaded points, retry-later on failure;
  * the pure merge priority: the user's own explicit rule > community
    override > built-in rules > nothing, plus the override lookup itself and
    its integration into apply_defaults.
"""
import asyncio
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent / "service"))

from app.config import settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.models import db_models  # noqa: E402,F401 - registers tables with Base
from app.models.food import FoodItem, FoodCategory, StorageType  # noqa: E402
from app.services import community_expiry, expiry_learning  # noqa: E402
from app.services.defaults import apply_defaults, is_seed_rule, seed_defaults  # noqa: E402


@pytest.fixture(autouse=True)
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    community_expiry.clear_cache()
    yield tmp_path
    expiry_learning.clear_queue()
    community_expiry.clear_cache()


def _seeded_db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    seed_defaults(db)
    return db


TODAY = date(2026, 7, 14)


# --- Signal detection (pure) --------------------------------------------------

def test_build_point_is_exactly_the_anonymous_fields():
    point = expiry_learning.build_point(
        "  Chobani  Greek Yogurt ", "0123456789012", "refrigerated",
        chosen_date=TODAY + timedelta(days=21),
        suggested_date=TODAY + timedelta(days=14),
        suggestion_source="default", base_date=TODAY)
    assert point == {
        "barcode": "0123456789012",
        "name_key": "chobani greek yogurt",
        "storage": "fridge",
        "shelf_life_days": 21,
        "suggested_days": 14,
        "suggestion_source": "default",
    }


def test_storage_kind_mapping():
    assert expiry_learning.storage_kind("refrigerated") == "fridge"
    assert expiry_learning.storage_kind("frozen") == "freezer"
    assert expiry_learning.storage_kind("dry") == "pantry"
    assert expiry_learning.storage_kind("room_temp") == "other"
    assert expiry_learning.storage_kind("wine_cellar") == "other"
    assert expiry_learning.storage_kind("") == "other"


def test_build_point_skips_a_confirmation_not_a_correction():
    same = TODAY + timedelta(days=14)
    assert expiry_learning.build_point(
        "Milk", None, "refrigerated", chosen_date=same, suggested_date=same,
        suggestion_source="default", base_date=TODAY) is None


def test_build_point_rejects_out_of_range_and_nameless():
    assert expiry_learning.build_point(
        "Milk", None, "refrigerated", chosen_date=TODAY,  # 0 days
        suggested_date=None, suggestion_source="none", base_date=TODAY) is None
    assert expiry_learning.build_point(
        "Milk", None, "refrigerated",
        chosen_date=TODAY + timedelta(days=5000),  # absurd window
        suggested_date=None, suggestion_source="none", base_date=TODAY) is None
    assert expiry_learning.build_point(
        "   ", None, "refrigerated", chosen_date=TODAY + timedelta(days=5),
        suggested_date=None, suggestion_source="none", base_date=TODAY) is None


def test_build_point_drops_store_local_and_garbage_barcodes():
    for code in ("212345678901", "0212345678905", "not-a-code", "123"):
        point = expiry_learning.build_point(
            "Deli ham", code, "refrigerated",
            chosen_date=TODAY + timedelta(days=5), suggested_date=None,
            suggestion_source="none", base_date=TODAY)
        assert point is not None
        assert point["barcode"] is None, code


def test_build_point_set_with_no_suggestion():
    point = expiry_learning.build_point(
        "Farm eggs", None, "refrigerated",
        chosen_date=TODAY + timedelta(days=30), suggested_date=None,
        suggestion_source="none", base_date=TODAY)
    assert point["suggested_days"] is None
    assert point["suggestion_source"] == "none"


# --- Consent gating and queue mechanics ---------------------------------------

def _record_sample():
    return expiry_learning.record(
        "Greek Yogurt", "0123456789012", "refrigerated",
        chosen_date=date.today() + timedelta(days=21),
        suggested_date=date.today() + timedelta(days=14),
        suggestion_source="default")


def test_nothing_is_captured_when_sharing_is_off(tmp_data_dir, monkeypatch):
    monkeypatch.setattr(settings, "share_expiry_learning", False, raising=False)
    assert _record_sample() is False
    assert expiry_learning.queued_points() == []
    assert not (tmp_data_dir / "expiry_learning_queue.json").exists()


def test_points_queue_when_sharing_is_on(tmp_data_dir, monkeypatch):
    monkeypatch.setattr(settings, "share_expiry_learning", True, raising=False)
    assert _record_sample() is True
    points = expiry_learning.queued_points()
    assert len(points) == 1
    assert points[0]["name_key"] == "greek yogurt"
    # The queue file holds exactly the anonymous points, nothing else.
    on_disk = json.loads((tmp_data_dir / "expiry_learning_queue.json").read_text())
    assert set(on_disk) == {"points"}


def test_queue_is_capped(monkeypatch):
    monkeypatch.setattr(settings, "share_expiry_learning", True, raising=False)
    monkeypatch.setattr(expiry_learning, "_MAX_QUEUE", 10)
    for i in range(15):
        expiry_learning.record(
            f"Item {i}", None, "dry",
            chosen_date=date.today() + timedelta(days=30 + i),
            suggested_date=None, suggestion_source="none")
    assert len(expiry_learning.queued_points()) == 10


class _StubClient:
    """A canned httpx.AsyncClient: records the request, answers a status."""
    sent = None
    status_code = 200
    raise_error = False

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        if type(self).raise_error:
            raise OSError("network down")
        type(self).sent = {"url": url, "json": json}
        import httpx
        return httpx.Response(type(self).status_code)


@pytest.fixture
def stub_upload(monkeypatch):
    _StubClient.sent = None
    _StubClient.status_code = 200
    _StubClient.raise_error = False
    monkeypatch.setattr(expiry_learning.httpx, "AsyncClient", _StubClient)
    return _StubClient


def test_flush_uploads_and_drains_the_queue(monkeypatch, stub_upload):
    monkeypatch.setattr(settings, "share_expiry_learning", True, raising=False)
    monkeypatch.setattr(settings, "cloud_base_url",
                        "https://forager.example", raising=False)
    assert _record_sample() is True
    result = asyncio.run(expiry_learning.flush())
    assert result == {"sent": 1}
    assert stub_upload.sent["url"] == "https://forager.example/api/learn/expiry"
    assert len(stub_upload.sent["json"]["points"]) == 1
    assert expiry_learning.queued_points() == []


def test_flush_keeps_the_queue_on_failure(monkeypatch, stub_upload):
    monkeypatch.setattr(settings, "share_expiry_learning", True, raising=False)
    monkeypatch.setattr(settings, "cloud_base_url",
                        "https://forager.example", raising=False)
    assert _record_sample() is True
    stub_upload.raise_error = True
    assert asyncio.run(expiry_learning.flush()) == {"sent": 0}
    assert len(expiry_learning.queued_points()) == 1  # retry later

    stub_upload.raise_error = False
    stub_upload.status_code = 500
    assert asyncio.run(expiry_learning.flush()) == {"sent": 0}
    assert len(expiry_learning.queued_points()) == 1  # retry later


def test_flush_discards_the_queue_when_consent_is_withdrawn(monkeypatch, stub_upload):
    monkeypatch.setattr(settings, "share_expiry_learning", True, raising=False)
    assert _record_sample() is True
    monkeypatch.setattr(settings, "share_expiry_learning", False, raising=False)
    assert asyncio.run(expiry_learning.flush()) == {"sent": 0}
    assert expiry_learning.queued_points() == []
    assert stub_upload.sent is None  # nothing left the machine


# --- Merge priority (pure) and the overrides lookup ---------------------------

def test_merge_days_priority():
    assert community_expiry.merge_days(7, 10, 14) == (7, "user")
    assert community_expiry.merge_days(None, 10, 14) == (10, "community")
    assert community_expiry.merge_days(None, None, 14) == (14, "default")
    assert community_expiry.merge_days(None, None, None) == (None, "none")


def _feed(entries):
    return {"version": 1, "generated_date": "2026-07-14", "overrides": entries}


def test_override_lookup_prefers_barcode_then_name():
    index = community_expiry.build_index(_feed([
        {"barcode": "0123456789012", "storage": "fridge",
         "days_median": 9, "samples": 12},
        {"name_key": "greek yogurt", "storage": "fridge",
         "days_median": 12, "samples": 30},
    ]))
    assert community_expiry.override_days(
        index, "Chobani thing", "0123456789012", "fridge") == 9
    assert community_expiry.override_days(
        index, "Greek  YOGURT", None, "fridge") == 12
    assert community_expiry.override_days(
        index, "Greek yogurt", None, "freezer") is None
    assert community_expiry.override_days(index, "unknown", None, "fridge") is None


def test_build_index_skips_malformed_and_absurd_entries():
    index = community_expiry.build_index(_feed([
        "not a dict",
        {"name_key": "ok", "storage": "fridge", "days_median": "eleven"},
        {"name_key": "absurd", "storage": "fridge", "days_median": 99999},
        {"name_key": "fine", "storage": "fridge", "days_median": 11,
         "samples": 6},
    ]))
    assert index == {("name", "fine", "fridge"): 11}


def _write_feed_cache(tmp_data_dir, entries):
    (tmp_data_dir / "community_expiry.json").write_text(json.dumps(_feed(entries)))


def test_apply_defaults_uses_a_community_override(tmp_data_dir, monkeypatch):
    monkeypatch.setattr(settings, "use_community_expiry", True, raising=False)
    db = _seeded_db()
    _write_feed_cache(tmp_data_dir, [
        {"name_key": "yogurt", "storage": "fridge", "days_median": 9,
         "samples": 20}])
    item = FoodItem(name="Yogurt", category=FoodCategory.dairy,
                    storage_type=StorageType.refrigerated)
    apply_defaults(item, db)
    assert (item.best_by_date - date.today()).days == 9  # not the seed's 14
    assert item.best_by_source == "community"


def test_apply_defaults_ignores_community_when_toggled_off(tmp_data_dir, monkeypatch):
    monkeypatch.setattr(settings, "use_community_expiry", False, raising=False)
    db = _seeded_db()
    _write_feed_cache(tmp_data_dir, [
        {"name_key": "yogurt", "storage": "fridge", "days_median": 9,
         "samples": 20}])
    item = FoodItem(name="Yogurt", category=FoodCategory.dairy,
                    storage_type=StorageType.refrigerated)
    apply_defaults(item, db)
    assert (item.best_by_date - date.today()).days == 14  # the seed rule
    assert item.best_by_source == "default"


def test_users_own_rule_beats_the_community_override(tmp_data_dir, monkeypatch):
    monkeypatch.setattr(settings, "use_community_expiry", True, raising=False)
    db = _seeded_db()
    # The user edited the seeded yogurt rule: it is now their own default.
    from app.models.db_models import ExpiryDefault
    rule = (db.query(ExpiryDefault)
            .filter_by(name_pattern="yogurt", storage_type="refrigerated")
            .first())
    rule.default_days = 20
    db.commit()
    assert not is_seed_rule(rule)
    _write_feed_cache(tmp_data_dir, [
        {"name_key": "yogurt", "storage": "fridge", "days_median": 9,
         "samples": 20}])
    item = FoodItem(name="Yogurt", category=FoodCategory.dairy,
                    storage_type=StorageType.refrigerated)
    apply_defaults(item, db)
    assert (item.best_by_date - date.today()).days == 20
    assert item.best_by_source == "default"


def test_is_seed_rule_distinguishes_user_rules():
    db = _seeded_db()
    from app.models.db_models import ExpiryDefault
    seeded = db.query(ExpiryDefault).filter_by(name_pattern="yogurt").first()
    assert is_seed_rule(seeded)
    own = ExpiryDefault(category="Dairy", name_pattern="oat milk",
                        storage_type="refrigerated", default_days=10, priority=1)
    assert not is_seed_rule(own)


def test_missing_cache_means_no_override(monkeypatch):
    monkeypatch.setattr(settings, "use_community_expiry", True, raising=False)
    assert community_expiry.suggested_days("Yogurt", None, "refrigerated") is None
