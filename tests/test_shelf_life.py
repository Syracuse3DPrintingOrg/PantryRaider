"""LLM shelf-life and storage estimate (FoodAssistant-ft92).

Covers the pure mapper (parse_llm_shelf_life / apply_shelf_life) and the way it
overrides the generic category default on the intake paths (barcode scan and
photo/receipt), while leaving behavior unchanged when the option is off.
"""
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import db_models  # noqa: F401 - registers tables with Base
from app.models.food import FoodItem, FoodCategory, StorageType
from app.services.defaults import seed_defaults, apply_defaults
from app.services.shelf_life import parse_llm_shelf_life, apply_shelf_life


def _seeded_db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    seed_defaults(db)
    return db


class _StubProvider:
    """A vision/enrich provider that returns a canned enrich_product reply."""

    def __init__(self, reply):
        self._reply = reply

    async def enrich_product(self, info):
        return self._reply

    async def identify_barcode(self, barcode):
        return self._reply


# --- parse_llm_shelf_life (pure) ------------------------------------------

def test_parse_good_answer():
    out = parse_llm_shelf_life({"shelf_life_days": 5, "storage_type": "refrigerated"})
    assert out == {"days": 5, "location": "refrigerated"}


def test_parse_missing_fields_returns_none():
    assert parse_llm_shelf_life({}) == {"days": None, "location": None}
    assert parse_llm_shelf_life({"unrelated": "value"}) == {"days": None, "location": None}


def test_parse_garbage_returns_none():
    assert parse_llm_shelf_life(None) == {"days": None, "location": None}
    assert parse_llm_shelf_life("not a dict") == {"days": None, "location": None}
    assert parse_llm_shelf_life({"shelf_life_days": "abc"}) == {"days": None, "location": None}


def test_parse_clamps_absurd_and_rejects_nonpositive():
    assert parse_llm_shelf_life({"shelf_life_days": 100000})["days"] == 3650
    assert parse_llm_shelf_life({"shelf_life_days": 0})["days"] is None
    assert parse_llm_shelf_life({"shelf_life_days": -5})["days"] is None
    # Floats coerce and clamp too.
    assert parse_llm_shelf_life({"shelf_life_days": 5.7})["days"] == 6


def test_parse_location_synonyms():
    def loc(v):
        return parse_llm_shelf_life({"storage_type": v})["location"]

    assert loc("keep refrigerated") == "refrigerated"
    assert loc("chilled") == "refrigerated"
    assert loc("fridge") == "refrigerated"
    assert loc("freezer") == "frozen"
    assert loc("keep frozen") == "frozen"
    assert loc("counter") == "room_temp"
    assert loc("pantry") == "dry"
    # Anything unrecognized falls back to the pantry (dry) bucket.
    assert loc("somewhere weird") == "dry"


def test_parse_alternate_field_names():
    out = parse_llm_shelf_life({"best_before_days": 12, "storage_location": "freezer"})
    assert out == {"days": 12, "location": "frozen"}


# --- apply_shelf_life onto a FoodItem -------------------------------------

def test_apply_sets_fields_and_returns_true():
    item = FoodItem(name="x", storage_type=StorageType.room_temp)
    got = apply_shelf_life(item, {"days": 5, "location": "refrigerated"})
    assert got is True
    assert item.storage_type == StorageType.refrigerated
    assert (item.best_by_date - date.today()).days == 5


def test_apply_no_days_returns_false_and_leaves_date():
    item = FoodItem(name="x", storage_type=StorageType.room_temp)
    got = apply_shelf_life(item, {"days": None, "location": None})
    assert got is False
    assert item.best_by_date is None
    assert item.storage_type == StorageType.room_temp


# --- override vs generic default (the cheesecake case) ---------------------

def test_llm_override_beats_generic_default_cheesecake():
    db = _seeded_db()
    # Without the LLM, the generic rule files this as its room-temp default.
    plain = FoodItem(name="Koriyama Cheese Mushipan",
                     category=FoodCategory.other, storage_type=StorageType.room_temp)
    apply_defaults(plain, db)
    assert plain.storage_type == StorageType.room_temp

    # With the LLM saying refrigerated + 5 days, that wins.
    item = FoodItem(name="Koriyama Cheese Mushipan",
                    category=FoodCategory.other, storage_type=StorageType.room_temp)
    apply_shelf_life(item, parse_llm_shelf_life(
        {"shelf_life_days": 5, "storage_type": "refrigerated"}))
    apply_defaults(item, db)  # must not overwrite the LLM answer
    assert item.storage_type == StorageType.refrigerated
    assert (item.best_by_date - date.today()).days == 5
    assert item.best_by_date != plain.best_by_date


# --- photo/receipt intake path (analyze._llm_shelf_life) -------------------

@pytest.mark.anyio
async def test_photo_intake_on_uses_llm_off_uses_default(monkeypatch):
    from app.routers import analyze
    db = _seeded_db()
    # A free-form location exercises the synonym mapping end to end.
    monkeypatch.setattr(
        "app.dependencies.get_enrich_provider",
        lambda: _StubProvider({"shelf_life_days": 5, "storage_type": "keep refrigerated"}))

    # ON: the router calls _llm_shelf_life, which overrides the default.
    on = FoodItem(name="mystery cheesecake",
                  category=FoodCategory.other, storage_type=StorageType.room_temp)
    await analyze._llm_shelf_life(on)
    apply_defaults(on, db)
    assert on.storage_type == StorageType.refrigerated
    assert (on.best_by_date - date.today()).days == 5

    # OFF: the router skips _llm_shelf_life, so only the generic default applies.
    off = FoodItem(name="mystery cheesecake",
                   category=FoodCategory.other, storage_type=StorageType.room_temp)
    apply_defaults(off, db)
    assert off.storage_type == StorageType.room_temp
    assert on.best_by_date != off.best_by_date


@pytest.mark.anyio
async def test_photo_intake_respects_printed_date(monkeypatch):
    from app.routers import analyze
    monkeypatch.setattr(
        "app.dependencies.get_enrich_provider",
        lambda: _StubProvider({"shelf_life_days": 5, "storage_type": "refrigerated"}))
    printed = date(2099, 1, 1)
    item = FoodItem(name="labelled milk", best_by_date=printed,
                    storage_type=StorageType.refrigerated)
    await analyze._llm_shelf_life(item)
    assert item.best_by_date == printed  # real packaging date is left alone


@pytest.mark.anyio
async def test_photo_intake_provider_error_falls_back_to_default(monkeypatch):
    from app.routers import analyze

    class _Boom:
        async def enrich_product(self, info):
            raise RuntimeError("provider down")

    monkeypatch.setattr("app.dependencies.get_enrich_provider", lambda: _Boom())
    db = _seeded_db()
    item = FoodItem(name="white rice", category=FoodCategory.grains,
                    storage_type=StorageType.dry)
    await analyze._llm_shelf_life(item)  # swallows the error, sets nothing
    assert item.best_by_date is None
    apply_defaults(item, db)
    assert item.best_by_date is not None  # generic default still applies


# --- barcode enrichment path (barcode._llm_enrich) -------------------------

@pytest.mark.anyio
async def test_barcode_enrich_uses_helper_when_enabled(monkeypatch):
    from app.config import settings
    from app.services import barcode
    monkeypatch.setattr(settings, "barcode_enrichment", "llm")
    monkeypatch.setattr(settings, "llm_expiry_enabled", True)
    monkeypatch.setattr(
        "app.dependencies.get_enrich_provider",
        lambda: _StubProvider({"name": "Cheesecake", "shelf_life_days": 5,
                               "storage_type": "chilled"}))
    item = FoodItem(name="orig", category=FoodCategory.other,
                    storage_type=StorageType.room_temp)
    ok = await barcode._llm_enrich(item, {"product_name": "orig"}, "", [])
    assert ok is True
    assert item.storage_type == StorageType.refrigerated  # "chilled" mapped
    assert (item.best_by_date - date.today()).days == 5


@pytest.mark.anyio
async def test_identify_barcode_null_name_is_not_found(monkeypatch):
    """When the model declines to guess an unknown barcode (name null), the
    fallback returns None so the caller reports "not found" rather than
    inventing a product (the Stella-scanned-as-Campbell's bug)."""
    from app.services import barcode
    monkeypatch.setattr("app.dependencies.get_enrich_provider",
                        lambda: _StubProvider({"name": None, "brand": None}))
    assert await barcode._llm_identify_barcode("018200261213") is None


@pytest.mark.anyio
async def test_identify_barcode_guess_is_flagged_unverified(monkeypatch):
    """A guess the model does commit to is accepted but plainly flagged and
    low-confidence, so it never reads like a confirmed scan."""
    from app.services import barcode
    monkeypatch.setattr(
        "app.dependencies.get_enrich_provider",
        lambda: _StubProvider({"name": "Heinz Ketchup", "brand": "Heinz",
                               "category": "Condiments", "storage_type": "room_temp"}))
    item = await barcode._llm_identify_barcode("012345678905")
    assert item is not None
    assert "unverified guess" in item.name.lower()
    assert item.confidence <= 0.25


@pytest.mark.anyio
async def test_identify_barcode_unsupported_provider_is_none(monkeypatch):
    """A provider with no identify_barcode (the base default returns None, as
    the cloud proxy does) yields no fabricated product."""
    from app.services import barcode

    class _NoIdentify:
        async def enrich_product(self, info):
            return {"name": "Whatever"}
    monkeypatch.setattr("app.dependencies.get_enrich_provider", lambda: _NoIdentify())
    assert await barcode._llm_identify_barcode("012345678905") is None


@pytest.mark.anyio
async def test_barcode_enrich_off_keeps_legacy_behavior(monkeypatch):
    from app.config import settings
    from app.services import barcode
    monkeypatch.setattr(settings, "barcode_enrichment", "llm")
    monkeypatch.setattr(settings, "llm_expiry_enabled", False)
    monkeypatch.setattr(
        "app.dependencies.get_enrich_provider",
        lambda: _StubProvider({"name": "Soup", "shelf_life_days": 400,
                               "storage_type": "dry"}))
    item = FoodItem(name="orig", category=FoodCategory.other,
                    storage_type=StorageType.room_temp)
    ok = await barcode._llm_enrich(item, {"product_name": "orig"}, "", [])
    assert ok is True
    # Legacy inline path still reads the canonical fields directly.
    assert item.storage_type == StorageType.dry
    assert (item.best_by_date - date.today()).days == 400
