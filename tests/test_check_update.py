"""Update check reads APP_VERSION from main, not just tags (FoodAssistant-jhug)."""
from __future__ import annotations

import sys
from pathlib import Path


SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.routers import admin  # noqa: E402
from app.config import APP_VERSION, settings  # noqa: E402


class _Resp:
    def __init__(self, status_code, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload if payload is not None else []

    def json(self):
        return self._payload


class _FakeClient:
    """Stand-in for httpx.AsyncClient: serves a canned config.py for the raw URL
    and a canned latest release for the releases API. Records requested URLs."""
    requested: list = []

    def __init__(self, raw_text=None, raw_status=200, release_tag=None):
        self._raw_text = raw_text
        self._raw_status = raw_status
        self._release_tag = release_tag

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        _FakeClient.requested.append(url)
        if "raw.githubusercontent.com" in url:
            if self._raw_text is None:
                return _Resp(404)
            return _Resp(self._raw_status, text=self._raw_text)
        if url.endswith("/releases/latest"):
            if self._release_tag is None:
                return _Resp(404, payload={})
            return _Resp(200, payload={"tag_name": self._release_tag})
        # tags fallback
        return _Resp(200, payload=[{"name": "v0.0.1"}])


def _patch_client(monkeypatch, **kw):
    import httpx
    _FakeClient.requested = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(**kw))


def test_detects_newer_version_on_main(monkeypatch):
    import asyncio
    _patch_client(monkeypatch, raw_text='APP_VERSION = "99.0.0"\n')
    out = asyncio.run(admin.check_update())
    assert out["ok"] is True
    assert out["latest"] == "99.0.0"
    assert out["update_available"] is True
    assert out["current"] == APP_VERSION


def test_same_version_is_not_an_update(monkeypatch):
    import asyncio
    _patch_client(monkeypatch, raw_text=f'APP_VERSION = "{APP_VERSION}"\n')
    out = asyncio.run(admin.check_update())
    assert out["ok"] is True
    assert out["latest"] == APP_VERSION
    assert out["update_available"] is False


def test_falls_back_to_tags_when_raw_unavailable(monkeypatch):
    import asyncio
    _patch_client(monkeypatch, raw_text=None)  # raw 404 -> tag fallback
    out = asyncio.run(admin.check_update())
    # The fake tag is v0.0.1, older than the running version: not an update,
    # but the call still succeeds via the fallback path.
    assert out["ok"] is True
    assert out["latest"] == "v0.0.1"
    assert out["update_available"] is False


# -- stable channel (FoodAssistant-wkwx) --------------------------------------

def test_stable_channel_compares_against_the_latest_release(monkeypatch):
    import asyncio
    monkeypatch.setattr(settings, "update_channel", "stable", raising=False)
    # main tip is far ahead, but the stable check must not read it at all.
    _patch_client(monkeypatch, raw_text='APP_VERSION = "99.0.0"\n',
                  release_tag="v98.0.0")
    out = asyncio.run(admin.check_update())
    assert out["ok"] is True
    assert out["latest"] == "v98.0.0"
    assert out["update_available"] is True
    assert out["release_url"].endswith("/releases/tag/v98.0.0")
    assert not any("raw.githubusercontent.com" in u for u in _FakeClient.requested)


def test_stable_channel_same_release_is_not_an_update(monkeypatch):
    import asyncio
    monkeypatch.setattr(settings, "update_channel", "stable", raising=False)
    _patch_client(monkeypatch, release_tag=f"v{APP_VERSION}")
    out = asyncio.run(admin.check_update())
    assert out["ok"] is True
    assert out["update_available"] is False


def test_stable_channel_falls_back_to_tags_without_a_release(monkeypatch):
    import asyncio
    monkeypatch.setattr(settings, "update_channel", "stable", raising=False)
    _patch_client(monkeypatch, release_tag=None)  # releases API 404 -> tags
    out = asyncio.run(admin.check_update())
    assert out["ok"] is True
    assert out["latest"] == "v0.0.1"
    assert out["update_available"] is False


def test_main_channel_does_not_ask_the_releases_api(monkeypatch):
    import asyncio
    monkeypatch.setattr(settings, "update_channel", "main", raising=False)
    _patch_client(monkeypatch, raw_text='APP_VERSION = "99.0.0"\n',
                  release_tag="v98.0.0")
    out = asyncio.run(admin.check_update())
    assert out["latest"] == "99.0.0"
    assert not any(u.endswith("/releases/latest") for u in _FakeClient.requested)
