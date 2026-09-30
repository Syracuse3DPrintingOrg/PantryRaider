"""Community shelf-life learning: submissions, validation, and the feed.

The endpoints are deliberately account-free (the app's sharing opt-in works
with or without a Forager account), so these tests never sign in. What they
guard: hard validation on the write path, the per-IP rate limit, the
k-threshold and agreement rules on the published feed, and the feed shape the
app's community_expiry client consumes.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.database import SessionLocal
from app.models import ExpiryObservation
from app.routers import learn


@pytest.fixture(autouse=True)
def fresh_feed_cache():
    learn.reset_feed_cache()
    yield
    learn.reset_feed_cache()


def _point(**over):
    base = {
        "barcode": "0123456789012",
        "name_key": "greek yogurt",
        "storage": "fridge",
        "shelf_life_days": 21,
        "suggested_days": 14,
        "suggestion_source": "default",
    }
    base.update(over)
    return base


def _rows():
    db = SessionLocal()
    try:
        return db.query(ExpiryObservation).all()
    finally:
        db.close()


# --- Submissions --------------------------------------------------------------

def test_valid_batch_is_stored_anonymously(client):
    resp = client.post("/api/learn/expiry", json={"points": [_point()]})
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1}
    rows = _rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.barcode == "0123456789012"
    assert row.name_key == "greek yogurt"
    assert row.storage == "fridge"
    assert row.shelf_life_days == 21
    assert row.suggested_days == 14
    assert row.suggestion_source == "default"
    # Day granularity only: a plain YYYY-MM-DD, nothing finer.
    assert len(row.received_date) == 10
    # Nothing identifying is stored: the model has no such columns at all.
    stored = {c.name for c in ExpiryObservation.__table__.columns}
    assert stored == {"id", "barcode", "name_key", "storage",
                      "shelf_life_days", "suggested_days",
                      "suggestion_source", "received_date"}


def test_name_key_is_renormalized_server_side(client):
    resp = client.post("/api/learn/expiry", json={
        "points": [_point(barcode=None, name_key="  Greek   YOGURT ")]})
    assert resp.status_code == 200
    assert _rows()[0].name_key == "greek yogurt"


def test_duplicate_points_in_one_batch_count_once(client):
    resp = client.post("/api/learn/expiry",
                       json={"points": [_point(), _point()]})
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1}
    assert len(_rows()) == 1


def test_empty_and_oversize_batches_are_rejected(client):
    assert client.post("/api/learn/expiry", json={"points": []}).status_code == 422
    too_many = [_point(shelf_life_days=d + 1) for d in range(learn.MAX_POINTS + 1)]
    assert client.post("/api/learn/expiry",
                       json={"points": too_many}).status_code == 422
    assert _rows() == []


@pytest.mark.parametrize("bad", [
    _point(name_key=""),                       # no product name
    _point(name_key="x" * 200),                # name too long
    _point(storage="garage"),                  # unknown storage kind
    _point(shelf_life_days=0),                 # below the believable window
    _point(shelf_life_days=5000),              # above the believable window
    _point(suggested_days=-1),                 # negative suggestion
    _point(suggestion_source="psychic"),       # unknown source
    _point(barcode="DROP TABLE"),              # non-numeric barcode
    _point(barcode="12345"),                   # implausibly short barcode
])
def test_invalid_points_reject_the_whole_batch(client, bad):
    resp = client.post("/api/learn/expiry",
                       json={"points": [_point(), bad]})
    assert resp.status_code == 422
    assert _rows() == []


def test_barcode_is_optional(client):
    resp = client.post("/api/learn/expiry",
                       json={"points": [_point(barcode=None)]})
    assert resp.status_code == 200
    assert _rows()[0].barcode is None


def test_submissions_are_rate_limited_per_ip(client, monkeypatch):
    monkeypatch.setattr(settings, "learn_rate_per_minute", 2)
    for i in range(2):
        assert client.post("/api/learn/expiry", json={
            "points": [_point(shelf_life_days=10 + i)]}).status_code == 200
    resp = client.post("/api/learn/expiry", json={"points": [_point()]})
    assert resp.status_code == 429


# --- Aggregation (pure) -------------------------------------------------------

def test_aggregate_enforces_the_k_threshold():
    rows = [{"barcode": None, "name_key": "milk", "storage": "fridge",
             "shelf_life_days": 10 + i} for i in range(4)]
    assert learn.aggregate(rows, k_threshold=5) == []
    rows.append({"barcode": None, "name_key": "milk", "storage": "fridge",
                 "shelf_life_days": 11})
    entries = learn.aggregate(rows, k_threshold=5)
    assert len(entries) == 1
    assert entries[0]["name_key"] == "milk"
    assert entries[0]["samples"] == 5
    assert entries[0]["days_median"] == 11


def test_aggregate_publishes_barcode_and_name_groups():
    rows = [{"barcode": "0123456789012", "name_key": "greek yogurt",
             "storage": "fridge", "shelf_life_days": 20 + (i % 3)}
            for i in range(6)]
    entries = learn.aggregate(rows, k_threshold=5)
    assert {e.get("barcode") for e in entries} == {"0123456789012", None}
    assert {e.get("name_key") for e in entries} == {None, "greek yogurt"}


def test_aggregate_drops_groups_with_absurd_spread():
    # Five kitchens that violently disagree do not make a confident median.
    rows = [{"barcode": None, "name_key": "mystery", "storage": "pantry",
             "shelf_life_days": d} for d in (2, 5, 30, 400, 3000)]
    assert learn.aggregate(rows, k_threshold=5) == []


def test_aggregate_keys_storage_separately():
    rows = ([{"barcode": None, "name_key": "chicken", "storage": "fridge",
              "shelf_life_days": 4} for _ in range(5)]
            + [{"barcode": None, "name_key": "chicken", "storage": "freezer",
                "shelf_life_days": 300} for _ in range(5)])
    entries = learn.aggregate(rows, k_threshold=5)
    days = {e["storage"]: e["days_median"] for e in entries}
    assert days == {"fridge": 4, "freezer": 300}


# --- The published feed -------------------------------------------------------

def test_overrides_feed_shape_and_threshold(client):
    # Four submissions: below the k-threshold, so the feed stays empty.
    for i in range(4):
        assert client.post("/api/learn/expiry", json={
            "points": [_point(barcode=None, suggested_days=None,
                              shelf_life_days=20 + i)]}).status_code == 200
    learn.reset_feed_cache()
    feed = client.get("/api/learn/expiry/overrides").json()
    assert feed["version"] == 1
    assert len(feed["generated_date"]) == 10
    assert feed["overrides"] == []

    # The fifth pushes the group over the threshold.
    assert client.post("/api/learn/expiry", json={
        "points": [_point(barcode=None, suggested_days=None,
                          shelf_life_days=22)]}).status_code == 200
    learn.reset_feed_cache()
    feed = client.get("/api/learn/expiry/overrides").json()
    assert len(feed["overrides"]) == 1
    entry = feed["overrides"][0]
    assert entry == {"name_key": "greek yogurt", "storage": "fridge",
                     "days_median": 22, "samples": 5}


def test_overrides_feed_is_cached_between_requests(client):
    learn.reset_feed_cache()
    first = client.get("/api/learn/expiry/overrides").json()
    # New data lands, but within the cache window the feed does not move.
    for i in range(5):
        client.post("/api/learn/expiry",
                    json={"points": [_point(shelf_life_days=20 + i)]})
    assert client.get("/api/learn/expiry/overrides").json() == first
