"""The single-value TTL cache behind the satellite GET /timers micro-cache
(FoodAssistant-3mq). Pure logic: time is injected, so nothing here sleeps."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services.ttl_cache import TTLCache  # noqa: E402


def test_empty_cache_returns_none():
    assert TTLCache(2.0).get(now=100.0) is None


def test_value_is_returned_within_the_ttl():
    c = TTLCache(2.0)
    c.set("timers-body", now=100.0)
    assert c.get(now=100.0) == "timers-body"
    assert c.get(now=101.9) == "timers-body"


def test_value_expires_at_the_ttl_boundary():
    c = TTLCache(2.0)
    c.set("timers-body", now=100.0)
    assert c.get(now=102.0) is None
    # Expiry is sticky: once dropped it stays gone even for an earlier clock.
    assert c.get(now=100.5) is None


def test_set_restarts_the_clock():
    c = TTLCache(2.0)
    c.set("old", now=100.0)
    c.set("new", now=101.5)
    assert c.get(now=103.0) == "new"
    assert c.get(now=103.5) is None


def test_invalidate_drops_the_value_immediately():
    c = TTLCache(2.0)
    c.set("timers-body", now=100.0)
    c.invalidate()
    assert c.get(now=100.0) is None


def test_none_is_indistinguishable_from_empty_by_design():
    # The router only caches real (status 200) response tuples, never None, so
    # a None sentinel is fine and keeps the API tiny.
    c = TTLCache(2.0)
    c.set(None, now=100.0)
    assert c.get(now=100.0) is None


def test_real_clock_is_the_default():
    c = TTLCache(60.0)
    c.set("x")
    assert c.get() == "x"


def test_nonpositive_ttl_is_rejected():
    with pytest.raises(ValueError):
        TTLCache(0)
    with pytest.raises(ValueError):
        TTLCache(-1.0)
