"""Client-side halves of the host-bridge token handshake (FoodAssistant-pxcm).

The bridge writes <data_dir>/bridge-token at startup; the app
(app/services/bridge.py) and the Stream Deck controller
(foodassistant_streamdeck/actions.py) read it back and send it as the
X-Bridge-Token header. These tests cover the read, the cache, the
missing-file first-boot case, and the 401-driven re-read.

Run: python -m pytest tests/test_bridge_token.py -q
"""
from __future__ import annotations

import httpx
import pytest

from app.services import bridge as app_bridge
from foodassistant_streamdeck import actions as deck_actions


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(app_bridge.settings, "data_dir", str(tmp_path))
    app_bridge.invalidate_bridge_token()
    yield tmp_path
    app_bridge.invalidate_bridge_token()


# --- App helper -------------------------------------------------------------

def test_app_headers_empty_before_bridge_writes(data_dir):
    assert app_bridge.bridge_headers() == {}


def test_app_reads_token_and_attaches_header(data_dir):
    (data_dir / "bridge-token").write_text("tok123\n")
    assert app_bridge.bridge_headers() == {"X-Bridge-Token": "tok123"}


def test_app_miss_is_not_cached(data_dir):
    # First boot: the app may look before the bridge has written the file.
    assert app_bridge.bridge_headers() == {}
    (data_dir / "bridge-token").write_text("late-token\n")
    assert app_bridge.bridge_headers() == {"X-Bridge-Token": "late-token"}


def test_app_token_is_cached_until_invalidated(data_dir):
    (data_dir / "bridge-token").write_text("first\n")
    assert app_bridge.bridge_token() == "first"
    (data_dir / "bridge-token").write_text("second\n")
    assert app_bridge.bridge_token() == "first"
    app_bridge.invalidate_bridge_token()
    assert app_bridge.bridge_token() == "second"


@pytest.mark.anyio
async def test_app_401_drops_cache_so_next_call_rereads(data_dir):
    (data_dir / "bridge-token").write_text("stale\n")
    assert app_bridge.bridge_token() == "stale"
    (data_dir / "bridge-token").write_text("rotated\n")
    resp = httpx.Response(401, request=httpx.Request("POST", "http://127.0.0.1:9299/reboot"))
    await app_bridge._drop_token_on_401(resp)
    assert app_bridge.bridge_token() == "rotated"


@pytest.mark.anyio
async def test_app_client_sends_the_header(data_dir):
    (data_dir / "bridge-token").write_text("clienttok\n")
    async with app_bridge.bridge_client(timeout=1.0) as c:
        assert c.headers.get("X-Bridge-Token") == "clienttok"
        assert app_bridge._drop_token_on_401 in c.event_hooks["response"]


# --- Stream Deck helper -----------------------------------------------------

@pytest.fixture
def deck_token(tmp_path):
    path = tmp_path / "bridge-token"
    deck_actions.invalidate_bridge_token(str(path))
    yield path
    deck_actions.invalidate_bridge_token(str(path))


def test_deck_headers_empty_when_file_missing(deck_token):
    assert deck_actions.bridge_headers(str(deck_token)) == {}


def test_deck_headers_read_token(deck_token):
    deck_token.write_text("decktok\n")
    assert deck_actions.bridge_headers(str(deck_token)) == {"X-Bridge-Token": "decktok"}


def test_deck_cache_and_invalidate(deck_token):
    deck_token.write_text("one\n")
    assert deck_actions.bridge_headers(str(deck_token))["X-Bridge-Token"] == "one"
    deck_token.write_text("two\n")
    assert deck_actions.bridge_headers(str(deck_token))["X-Bridge-Token"] == "one"
    deck_actions.invalidate_bridge_token(str(deck_token))
    assert deck_actions.bridge_headers(str(deck_token))["X-Bridge-Token"] == "two"


@pytest.mark.anyio
async def test_deck_bridge_post_attaches_header_and_drops_cache_on_401(deck_token):
    deck_token.write_text("decktok\n")
    seen = {}

    class FakeClient:
        async def post(self, url, **kwargs):
            seen["headers"] = kwargs.get("headers") or {}
            return httpx.Response(401, request=httpx.Request("POST", url))

    face = await deck_actions.bridge_post(
        FakeClient(), "http://127.0.0.1:9299", "/reboot", token_path=str(deck_token))
    assert face == "Failed"
    assert seen["headers"].get("X-Bridge-Token") == "decktok"
    # The 401 dropped the cache: a rewritten file is picked up next call.
    deck_token.write_text("fresh\n")
    assert deck_actions.bridge_headers(str(deck_token))["X-Bridge-Token"] == "fresh"
