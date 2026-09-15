"""The rate-limiter dict must not grow without bound.

Several call sites key on the raw client address with no sign-in in front, so
the caller picks the key space, and over IPv6 that space is effectively
unlimited. A key whose own window has rolled over carries no information, so it
is swept out; past a hard ceiling a brand new key is denied rather than stored.
The budgets themselves, including the slower per-hour ones, are unchanged.
"""
from app import ratelimit


def setup_function():
    ratelimit.reset()


def teardown_function():
    ratelimit.reset()


def test_a_rolled_over_key_is_evicted_not_just_reset():
    base = 1_000_000.0
    assert ratelimit.allow("ip:1.2.3.4", 5, now=base) is True
    assert len(ratelimit._windows) == 1
    # Enough later calls from other keys to trigger a sweep, a full window on.
    for i in range(ratelimit._SWEEP_EVERY):
        ratelimit.allow(f"ip:10.0.0.{i}", 5, now=base + 3600)
    assert "ip:1.2.3.4" not in ratelimit._windows


def test_the_current_window_is_never_swept_away():
    base = 1_000_000.0
    assert ratelimit.allow("ip:1.2.3.4", 1, now=base) is True
    for i in range(ratelimit._SWEEP_EVERY):
        ratelimit.allow(f"ip:10.0.0.{i}", 5, now=base)
    # Still capped: the sweep must not hand a live key a fresh count.
    assert ratelimit.allow("ip:1.2.3.4", 1, now=base) is False


def test_an_hourly_budget_is_not_swept_by_minute_traffic():
    """Share emails are capped per hour. A sweep driven by per-minute traffic
    must not forget them, which would turn the hourly cap into no cap."""
    base = 1_000_000.0
    assert ratelimit.allow("share:acct", 1, now=base, window_seconds=3600) is True
    for i in range(ratelimit._SWEEP_EVERY * 2):
        ratelimit.allow(f"ip:10.0.0.{i}", 50, now=base + 120)
    assert ratelimit.allow("share:acct", 1, now=base + 120,
                           window_seconds=3600) is False
    # And it does roll over once its own hour is up.
    assert ratelimit.allow("share:acct", 1, now=base + 3700,
                           window_seconds=3600) is True


def test_a_new_key_is_denied_at_the_ceiling_rather_than_stored(monkeypatch):
    monkeypatch.setattr(ratelimit, "_MAX_KEYS", 4)
    base = 1_000_000.0
    for i in range(4):
        assert ratelimit.allow(f"ip:10.0.0.{i}", 5, now=base) is True
    assert ratelimit.allow("ip:10.0.0.99", 5, now=base) is False
    assert len(ratelimit._windows) <= 4
    # A key already resident keeps its own budget.
    assert ratelimit.allow("ip:10.0.0.0", 5, now=base) is True


def test_reset_still_empties_everything():
    ratelimit.allow("ip:1.2.3.4", 5)
    ratelimit.reset()
    assert ratelimit._windows == {}
