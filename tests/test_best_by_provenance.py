"""Unit tests for the best-by provenance store (FoodAssistant-cidz).

The store maps a product id (and a normalized name fallback) to the source of
a best-by date, but only trusts that record while the stored date still
matches the item's current best-by: a mismatch means the date was since
changed (e.g. edited directly in Grocy), so the record is stale and the
lookup falls back to "manual" (no badge).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "service"))

from app.services import best_by_provenance as bbp  # noqa: E402


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    bbp.clear_all()
    yield
    bbp.clear_all()


def test_normalize_name_folds_case_and_whitespace():
    assert bbp.normalize_name("  Chicken   Stock  ") == "chicken stock"
    assert bbp.normalize_name("") == ""


def test_record_and_lookup_round_trip_by_id():
    bbp.record(42, "Chicken Stock", "default", "2026-07-22")
    assert bbp.lookup(42, "Chicken Stock", "2026-07-22") == "default"


def test_record_and_lookup_round_trip_by_name_fallback():
    # No product id yet at record time; looked up later by name alone.
    bbp.record(None, "Chicken Stock", "llm", "2026-07-22")
    assert bbp.lookup(None, "Chicken Stock", "2026-07-22") == "llm"
    # Case/whitespace-insensitive.
    assert bbp.lookup(None, "  chicken stock  ", "2026-07-22") == "llm"


def test_stale_date_falls_back_to_manual():
    bbp.record(42, "Chicken Stock", "default", "2026-07-22")
    # The date on record no longer matches today's best-by (edited in Grocy).
    assert bbp.lookup(42, "Chicken Stock", "2026-08-01") == "manual"


def test_missing_record_is_manual():
    assert bbp.lookup(99, "Never Recorded", "2026-07-22") == "manual"


def test_missing_current_date_is_manual():
    bbp.record(42, "Chicken Stock", "default", "2026-07-22")
    assert bbp.lookup(42, "Chicken Stock", "") == "manual"


def test_recording_manual_source_is_a_noop():
    # "manual" is the no-badge default anyway; nothing needs to be stored.
    bbp.record(7, "Milk", "manual", "2026-07-10")
    assert bbp.lookup(7, "Milk", "2026-07-10") == "manual"


def test_unknown_source_is_treated_as_manual():
    bbp.record(7, "Milk", "guessed-by-a-wizard", "2026-07-10")
    assert bbp.lookup(7, "Milk", "2026-07-10") == "manual"


def test_id_lookup_preferred_over_name_when_both_present():
    bbp.record(1, "Milk", "default", "2026-07-10")
    # A different item happens to share the normalized name and overwrites
    # the name-fallback key with a different source.
    bbp.record(None, "Milk", "llm", "2026-07-10")
    # id=1's own record still wins over the (now stale-for-it) name fallback.
    assert bbp.lookup(1, "Milk", "2026-07-10") == "default"


def test_clear_all_drops_everything():
    bbp.record(1, "Milk", "default", "2026-07-10")
    bbp.clear_all()
    assert bbp.lookup(1, "Milk", "2026-07-10") == "manual"
