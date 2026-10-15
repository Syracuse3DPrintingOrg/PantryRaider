"""How the OpenAI provider handles a rate limit (429) and a refused key (401).

A 429 is OpenAI asking for a pause, not a dead key: the provider waits as long
as Retry-After says (capped) and tries the same key again before a spare. A
401 means the key is not valid: the provider stops using it, raises one Review
inbox item so the owner knows, and only then carries on with a spare.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "service"))

from app.providers import openai as oa  # noqa: E402

OK = {"choices": [{"message": {"content": '{"ok": true}'}}]}


def _setup(monkeypatch, replies):
    """replies: list of (status, headers) consumed in order; records each call."""
    calls, sleeps, items = [], [], []
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers["authorization"].split()[-1]
        calls.append(key)
        status, headers = queue.pop(0)
        return httpx.Response(status, headers=headers, json=OK if status == 200 else {})

    real_client = httpx.AsyncClient

    def client(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real_client(*a, **kw)

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(oa.httpx, "AsyncClient", client)
    monkeypatch.setattr(oa.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(oa, "_raise_key_rejected_item",
                        lambda key, has_spare: items.append((key, has_spare)))
    return calls, sleeps, items


def _provider(*spares):
    return oa.OpenAIProvider("primary-key-1111", model="gpt-4o", extra_keys=list(spares))


def _run(p):
    return asyncio.run(p._generate("hi"))


def test_429_waits_and_retries_the_same_key(monkeypatch):
    calls, sleeps, items = _setup(monkeypatch, [(429, {"retry-after": "3"}), (200, {})])
    p = _provider("spare-key-2222")
    assert _run(p) == '{"ok": true}'
    assert calls == ["primary-key-1111", "primary-key-1111"]
    assert sleeps == [3.0]
    assert p.api_key == "primary-key-1111" and not items


def test_429_twice_moves_to_a_spare(monkeypatch):
    calls, sleeps, _ = _setup(monkeypatch, [(429, {}), (429, {}), (200, {})])
    p = _provider("spare-key-2222")
    _run(p)
    assert calls == ["primary-key-1111", "primary-key-1111", "spare-key-2222"]
    assert sleeps == [oa._RETRY_AFTER_DEFAULT_S]


def test_429_on_the_only_key_raises_after_one_retry(monkeypatch):
    calls, _, _ = _setup(monkeypatch, [(429, {}), (429, {})])
    with pytest.raises(httpx.HTTPStatusError):
        _run(_provider())
    assert len(calls) == 2


def test_401_marks_the_key_bad_and_raises_an_inbox_item(monkeypatch):
    calls, sleeps, items = _setup(monkeypatch, [(401, {}), (200, {}), (200, {})])
    p = _provider("spare-key-2222")
    _run(p)
    assert calls == ["primary-key-1111", "spare-key-2222"]
    assert items == [("primary-key-1111", True)]
    assert not sleeps
    # The bad key is not tried again, and the inbox item is not raised twice.
    _run(p)
    assert calls[-1] == "spare-key-2222" and len(calls) == 3
    assert len(items) == 1


def test_401_on_the_only_key_says_to_replace_it(monkeypatch):
    calls, _, items = _setup(monkeypatch, [(401, {})])
    p = _provider()
    with pytest.raises(httpx.HTTPStatusError):
        _run(p)
    assert items == [("primary-key-1111", False)]
    with pytest.raises(RuntimeError, match="Replace it"):
        _run(p)
    assert len(calls) == 1


@pytest.mark.parametrize("headers,expected", [
    ({"retry-after": "5"}, 5.0),
    ({"retry-after": "0.5"}, 0.5),
    ({"retry-after-ms": "1500", "retry-after": "9"}, 1.5),
    ({"retry-after": "600"}, oa._RETRY_AFTER_CAP_S),
    ({"retry-after": "-4"}, 0.0),
    ({"retry-after": "soon"}, oa._RETRY_AFTER_DEFAULT_S),
    ({}, oa._RETRY_AFTER_DEFAULT_S),
    ({"retry-after": "Thu, 01 Jan 1970 00:00:10 GMT"}, 4.0),
])
def test_retry_after_parsing(headers, expected):
    assert oa.retry_after_seconds(headers, now=6.0) == pytest.approx(expected)


def test_inbox_item_names_only_the_key_tail(monkeypatch):
    seen = {}

    class _DB:
        def close(self):
            pass

    import app.database as database
    from app.services import action_items
    monkeypatch.setattr(database, "SessionLocal", lambda: _DB())
    monkeypatch.setattr(action_items, "create",
                        lambda db, kind, title, **kw: seen.update(kind=kind, title=title, **kw))
    oa._raise_key_rejected_item("sk-secret-abcd", has_spare=False)
    assert seen["kind"] == oa.KEY_REJECTED_KIND
    assert "abcd" in seen["body"] and "sk-secret" not in seen["body"]
    assert "sk-secret" not in seen["dedupe_key"]
    assert "Settings, Scanning & AI" in seen["body"]
