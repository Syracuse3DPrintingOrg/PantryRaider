"""On-screen update popup: decision helper + the update-notice route
(FoodAssistant-5wtc)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path


SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.routers import admin  # noqa: E402
from app.config import APP_VERSION, settings  # noqa: E402


# -- pure decision helper -----------------------------------------------------

def test_should_notify_when_available_and_not_dismissed():
    assert admin.should_notify_update(True, "9.9.9", "") is True
    assert admin.should_notify_update(True, "9.9.9", "0.1.0") is True


def test_no_notify_when_not_available():
    assert admin.should_notify_update(False, "9.9.9", "") is False


def test_no_notify_when_dismissed_same_version():
    assert admin.should_notify_update(True, "9.9.9", "9.9.9") is False
    # A leading "v" and equal numbers still count as the same dismissed version.
    assert admin.should_notify_update(True, "v9.9.9", "9.9.9") is False


def test_notify_again_for_a_newer_version_than_dismissed():
    # Dismissed 9.9.9 earlier; a newer 9.9.10 must pop again.
    assert admin.should_notify_update(True, "9.9.10", "9.9.9") is True


def test_no_notify_without_a_latest_version():
    assert admin.should_notify_update(True, "", "") is False


# -- the update-notice route --------------------------------------------------

def _seed_cache(monkeypatch, latest, available, age_seconds=10.0):
    import time
    monkeypatch.setattr(settings, "update_last_checked", time.time() - age_seconds,
                        raising=False)
    monkeypatch.setattr(settings, "update_last_latest", latest, raising=False)
    monkeypatch.setattr(settings, "update_last_available", available, raising=False)


def test_notice_uses_recent_cache_and_does_not_call_github(monkeypatch):
    # If check_update were called it would raise, proving the cache path is used.
    def _boom():
        raise AssertionError("check_update must not run when the cache is fresh")
    monkeypatch.setattr(admin, "check_update", _boom)
    _seed_cache(monkeypatch, "99.0.0", True)
    out = asyncio.run(admin.update_notice(dismissed="", prefer_cache=True))
    assert out["ok"] is True
    assert out["show"] is True
    assert out["latest"] == "99.0.0"
    assert out["update_available"] is True


def test_notice_hides_when_this_version_was_dismissed(monkeypatch):
    _seed_cache(monkeypatch, "99.0.0", True)
    out = asyncio.run(admin.update_notice(dismissed="99.0.0", prefer_cache=True))
    assert out["ok"] is True
    assert out["show"] is False


def test_notice_no_show_when_not_available(monkeypatch):
    _seed_cache(monkeypatch, APP_VERSION, False)
    out = asyncio.run(admin.update_notice(dismissed="", prefer_cache=True))
    assert out["show"] is False


def test_notice_reports_pi_appliance_flag(monkeypatch):
    _seed_cache(monkeypatch, "99.0.0", True)
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted", raising=False)
    out = asyncio.run(admin.update_notice(prefer_cache=True))
    assert out["is_pi_appliance"] is True
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    out = asyncio.run(admin.update_notice(prefer_cache=True))
    assert out["is_pi_appliance"] is False


def test_notice_falls_back_to_live_check_when_cache_stale(monkeypatch):
    # No usable cache: prefer_cache=False forces the live check, which we stub.
    async def _fake_check():
        return {"ok": True, "current": APP_VERSION, "latest": "5.0.0",
                "update_available": True, "checked_at": 0,
                "release_url": "https://example.test/rel"}
    monkeypatch.setattr(admin, "check_update", _fake_check)
    out = asyncio.run(admin.update_notice(dismissed="", prefer_cache=False))
    assert out["show"] is True
    assert out["latest"] == "5.0.0"
    assert out["release_url"] == "https://example.test/rel"


def test_notice_quiet_when_check_fails(monkeypatch):
    async def _fail_check():
        return {"ok": False, "current": APP_VERSION, "error": "offline"}
    monkeypatch.setattr(admin, "check_update", _fail_check)
    out = asyncio.run(admin.update_notice(dismissed="", prefer_cache=False))
    assert out["ok"] is False
    assert out["show"] is False
