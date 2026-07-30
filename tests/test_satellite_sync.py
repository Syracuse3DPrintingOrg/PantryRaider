"""Tests for the satellite's periodic background re-sync loop.

The loop in app.main re-pulls backend config from the main server every
settings.satellite_sync_minutes (0 disables). These tests drive the loop with a
fake asyncio.sleep so they finish instantly.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app import main as main_mod  # noqa: E402
from app.services import satellite as sat  # noqa: E402


def test_sync_minutes_default_and_saveable():
    from app.config import _SAVEABLE
    assert settings.satellite_sync_minutes == 15
    assert "satellite_sync_minutes" in _SAVEABLE


def _run_loop_with_fake_sleep(monkeypatch, stop_after):
    """Run the periodic loop, breaking out after `stop_after` sleeps."""
    state = {"sleeps": 0}

    async def fake_sleep(_seconds):
        state["sleeps"] += 1
        if state["sleeps"] >= stop_after:
            raise asyncio.CancelledError()

    monkeypatch.setattr(main_mod.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main_mod._periodic_satellite_sync())


def test_periodic_sync_calls_upstream_when_enabled(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(sat, "sync_from_upstream", lambda: calls.__setitem__("n", calls["n"] + 1))
    monkeypatch.setattr(settings, "satellite_sync_minutes", 5)
    _run_loop_with_fake_sleep(monkeypatch, stop_after=3)
    assert calls["n"] >= 1


def test_periodic_sync_skips_when_disabled(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(sat, "sync_from_upstream", lambda: calls.__setitem__("n", calls["n"] + 1))
    monkeypatch.setattr(settings, "satellite_sync_minutes", 0)
    _run_loop_with_fake_sleep(monkeypatch, stop_after=2)
    assert calls["n"] == 0
