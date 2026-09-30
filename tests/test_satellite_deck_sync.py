"""The satellite sync's Stream Deck push never wipes the deck's own layout.

_push_streamdeck_settings reads the deck config from the host bridge, overlays
the synced fields, and posts the result back. When the read fails (a corrupt
config.toml answers 500, an older bridge 404) there is nothing safe to merge
onto, so it must skip the write instead of posting just the synced fields.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import satellite as sat  # noqa: E402


class _Resp:
    def __init__(self, status_code: int, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


@pytest.fixture
def deck_pi(monkeypatch):
    monkeypatch.setattr("app.hardware.is_raspberry_pi", lambda: True)
    monkeypatch.setattr(settings, "has_streamdeck", True)
    monkeypatch.setattr(sat, "bridge_headers", lambda: {})


def _capture(monkeypatch, get_resp):
    posts: list[dict] = []
    invalidated: list[bool] = []
    monkeypatch.setattr(sat.httpx, "get", lambda *a, **k: get_resp)

    def fake_post(url, json=None, **k):
        posts.append(json)
        return _Resp(200, {"ok": True})

    monkeypatch.setattr(sat.httpx, "post", fake_post)
    monkeypatch.setattr(sat, "invalidate_bridge_token", lambda: invalidated.append(True))
    return posts, invalidated


@pytest.mark.parametrize("status", [500, 404, 503])
def test_bridge_read_failure_skips_the_write(deck_pi, monkeypatch, status):
    posts, _ = _capture(monkeypatch, _Resp(status, {"ok": False, "error": "bad toml"}))
    assert sat._push_streamdeck_settings() is False
    assert posts == []


def test_bridge_read_401_drops_the_cached_token(deck_pi, monkeypatch):
    posts, invalidated = _capture(monkeypatch, _Resp(401, {"error": "unauthorized"}))
    assert sat._push_streamdeck_settings() is False
    assert posts == []
    assert invalidated == [True]


def test_bridge_read_without_config_skips_the_write(deck_pi, monkeypatch):
    posts, _ = _capture(monkeypatch, _Resp(200, {"ok": True}))
    assert sat._push_streamdeck_settings() is False
    assert posts == []


def test_successful_read_merges_onto_the_device_layout(deck_pi, monkeypatch):
    device = {"keys": ["pending", "commit"], "rotation": 180,
              "base_url": "http://server:9284", "brightness": 40}
    posts, _ = _capture(monkeypatch, _Resp(200, {"ok": True, "config": dict(device)}))
    assert sat._push_streamdeck_settings() is True
    assert len(posts) == 1
    sent = posts[0]["config"]
    assert sent["keys"] == ["pending", "commit"]
    assert sent["base_url"] == "http://server:9284"
    assert sent["brightness"] == 40
    assert sent["rotation"] == 180
