"""Per-recipe cook counts: identity helper + store (FoodAssistant-bjps)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.database import SessionLocal  # noqa: E402
from app.models.db_models import RecipeCookCount  # noqa: E402
from app.services import cook_counts as cc  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_table():
    db = SessionLocal()
    db.query(RecipeCookCount).delete()
    db.commit()
    db.close()
    yield
    db = SessionLocal()
    db.query(RecipeCookCount).delete()
    db.commit()
    db.close()


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


# -- identity helper (pure) -------------------------------------------------

def test_same_recipe_same_source_same_key():
    a = cc.cook_identity("mealie", slug="chicken-soup")
    b = cc.cook_identity("mealie", slug="chicken-soup")
    assert a == b == "mealie:chicken-soup"


def test_same_id_different_sources_are_distinct():
    m = cc.cook_identity("mealie", slug="42")
    t = cc.cook_identity("themealdb", external_id="42")
    f = cc.cook_identity("forager", external_id="42")
    assert len({m, t, f}) == 3


def test_slug_preferred_over_external_id_but_both_key():
    assert cc.cook_identity("themealdb", external_id="777") == "themealdb:777"
    assert cc.cook_identity("mealie", slug="s", external_id="x") == "mealie:s"


def test_title_fallback_when_no_id_and_is_normalized():
    a = cc.cook_identity("mealie", title="Chicken Soup!")
    b = cc.cook_identity("mealie", title="chicken   soup")
    assert a == b == "mealie:t:chicken soup"


def test_no_id_no_title_is_empty():
    assert cc.cook_identity("mealie") == ""


def test_key_for_recipe_browse_and_current_shapes():
    # A browse row (source + slug + name) and a Current Recipe dict (source + id
    # + title) for the same recipe resolve to the same key.
    browse = {"source": "mealie", "slug": "pad-thai", "name": "Pad Thai"}
    current = {"source": "mealie", "id": "pad-thai", "title": "Pad Thai"}
    assert cc.key_for_recipe(browse) == cc.key_for_recipe(current) == "mealie:pad-thai"


# -- store ------------------------------------------------------------------

def test_record_cook_increments(db):
    r1 = cc.record_cook(db, "mealie", slug="stew", title="Beef Stew")
    assert r1["count"] == 1 and r1["last_cooked_at"]
    r2 = cc.record_cook(db, "mealie", slug="stew", title="Beef Stew")
    assert r2["count"] == 2
    # Same identity from a different surface keeps the same counter.
    r3 = cc.record_cook(db, "mealie", external_id=None, slug="stew")
    assert r3["count"] == 3


def test_record_cook_no_identity_is_noop(db):
    assert cc.record_cook(db, "mealie") is None
    assert db.query(RecipeCookCount).count() == 0


def test_counts_for_batch(db):
    cc.record_cook(db, "mealie", slug="a", title="A")
    cc.record_cook(db, "themealdb", external_id="9", title="Nine")
    keys = ["mealie:a", "themealdb:9", "mealie:never"]
    counts = cc.counts_for(db, keys)
    assert counts["mealie:a"]["count"] == 1
    assert counts["themealdb:9"]["count"] == 1
    # A never-cooked recipe is simply absent from the batch result.
    assert "mealie:never" not in counts


def test_counts_for_empty_keys(db):
    assert cc.counts_for(db, []) == {}
    assert cc.counts_for(db, ["", None]) == {}


def test_annotate_attaches_counts_and_never_cooked_shows_zero(db):
    cc.record_cook(db, "mealie", slug="made", title="Made")
    recipes = [
        {"source": "mealie", "slug": "made", "name": "Made"},
        {"source": "mealie", "slug": "fresh", "name": "Fresh"},
    ]
    cc.annotate(db, recipes)
    assert recipes[0]["cook_count"] == 1
    assert recipes[0]["last_cooked_at"]
    # Never cooked: count 0 and no last-cooked stamp.
    assert recipes[1]["cook_count"] == 0
    assert "last_cooked_at" not in recipes[1]
