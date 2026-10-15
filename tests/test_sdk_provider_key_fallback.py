"""Spare API keys on the Gemini and Anthropic providers.

The same rules as the OpenAI provider (tests/test_openai_key_fallback.py): a
rate-limited key gives way to a spare, a refused key is dropped with one Review
inbox item, and when every key fails the last error reaches the caller as it
always did. Each SDK client is a fake keyed by its API key, so no network is
needed.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "service"))

anthropic = pytest.importorskip("anthropic")
genai_errors = pytest.importorskip("google.genai.errors")

from app.providers import anthropic as ap  # noqa: E402
from app.providers import base  # noqa: E402
from app.providers import gemini as gp  # noqa: E402

PRIMARY, SPARE, THIRD = "primary-key-1111", "spare-key-2222", "third-key-3333"


# Errors as each SDK raises them.

def _anthropic_error(status: int):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request)
    cls = {401: anthropic.AuthenticationError, 429: anthropic.RateLimitError,
           500: anthropic.InternalServerError}[status]
    return cls(f"HTTP {status}", response=response, body=None)


def _gemini_error(status: int, details=None):
    names = {400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED",
             429: "RESOURCE_EXHAUSTED", 500: "INTERNAL"}
    body = {"error": {"code": status, "message": f"HTTP {status}",
                      "status": names[status], "details": details or []}}
    cls = genai_errors.ClientError if status < 500 else genai_errors.ServerError
    return cls(status, body, None)


GEMINI_BAD_KEY = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                   "reason": "API_KEY_INVALID", "domain": "googleapis.com"}]


# Fakes: one client per key, all drawing replies from one shared queue.

class _Script:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[str] = []

    def next(self, key):
        self.calls.append(key)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _anthropic_client(script, key):
    async def create(**kwargs):
        script.next(key)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text='{"ok": true}')],
            stop_reason="end_turn", usage=None)
    return types.SimpleNamespace(messages=types.SimpleNamespace(create=create))


def _gemini_client(script, key):
    async def generate_content(**kwargs):
        script.next(key)
        return types.SimpleNamespace(text='{"ok": true}', usage_metadata=None)
    models = types.SimpleNamespace(generate_content=generate_content)
    return types.SimpleNamespace(aio=types.SimpleNamespace(models=models))


def _setup(monkeypatch, which, replies, *spares):
    script = _Script(replies)
    sleeps, items = [], []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(base.asyncio, "sleep", fake_sleep)
    if which == "anthropic":
        mod, make, cls, model = ap, _anthropic_client, ap.AnthropicProvider, "claude-test"
    else:
        mod, make, cls, model = gp, _gemini_client, gp.GeminiProvider, "gemini-test"
    monkeypatch.setattr(mod, "_raise_key_rejected_item",
                        lambda key, has_spare: items.append((key, has_spare)))
    monkeypatch.setattr(cls, "_make_client", lambda self, key: make(script, key))
    p = cls(PRIMARY, model=model, extra_keys=list(spares))
    return p, script, sleeps, items


def _run(p):
    if isinstance(p, ap.AnthropicProvider):
        return asyncio.run(p._generate("hi"))
    return asyncio.run(p._gen(["hi"])).text


def _error(which, status, details=None):
    return _anthropic_error(status) if which == "anthropic" else _gemini_error(status, details)


def _rejected(which):
    """The error each provider sends for a key it does not accept."""
    if which == "anthropic":
        return _anthropic_error(401)
    return _gemini_error(400, GEMINI_BAD_KEY)


OK = object()
BOTH = pytest.mark.parametrize("which", ["anthropic", "gemini"])


@BOTH
def test_rate_limit_on_the_first_key_uses_the_second(monkeypatch, which):
    # Gemini's SDK does not retry, so the provider waits and tries the same
    # key once more; the Anthropic SDK already did that before raising.
    first = [_error(which, 429)] * (2 if which == "gemini" else 1)
    p, script, sleeps, items = _setup(monkeypatch, which, [*first, OK, OK], SPARE)
    assert _run(p) == '{"ok": true}'
    assert script.calls == [PRIMARY] * len(first) + [SPARE]
    assert len(sleeps) == len(first) - 1
    assert not items
    # The working spare is now the one tried first.
    assert p.api_key == SPARE and p.extra_keys == [PRIMARY]
    _run(p)
    assert script.calls[-1] == SPARE


def test_gemini_rate_limit_waits_as_asked_and_retries_the_same_key(monkeypatch):
    delay = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}]
    p, script, sleeps, _ = _setup(
        monkeypatch, "gemini", [_gemini_error(429, delay), OK], SPARE)
    _run(p)
    assert script.calls == [PRIMARY, PRIMARY]
    assert sleeps == [7.0]
    assert p.api_key == PRIMARY


@BOTH
def test_refused_key_raises_one_inbox_item_and_uses_the_next(monkeypatch, which):
    p, script, sleeps, items = _setup(
        monkeypatch, which, [_rejected(which), OK, OK], SPARE, THIRD)
    _run(p)
    assert script.calls == [PRIMARY, SPARE]
    assert items == [(PRIMARY, True)]
    assert not sleeps
    # The refused key is not tried again, and the inbox item is not repeated.
    _run(p)
    assert script.calls == [PRIMARY, SPARE, SPARE]
    assert len(items) == 1


@BOTH
def test_every_key_failing_raises_the_last_error(monkeypatch, which):
    p, script, _, items = _setup(
        monkeypatch, which, [_rejected(which), _rejected(which)], SPARE)
    with pytest.raises(Exception) as caught:
        _run(p)
    assert type(caught.value) is type(_rejected(which))
    assert items == [(PRIMARY, True), (SPARE, False)]
    # With every key refused, the next call says to replace the key.
    with pytest.raises(RuntimeError, match="Replace it in Settings, Scanning & AI"):
        _run(p)
    assert len(script.calls) == 2


@BOTH
def test_rate_limit_on_every_key_raises_the_rate_limit(monkeypatch, which):
    per_key = 2 if which == "gemini" else 1
    p, script, _, items = _setup(
        monkeypatch, which, [_error(which, 429)] * (2 * per_key), SPARE)
    with pytest.raises(Exception) as caught:
        _run(p)
    assert type(caught.value) is type(_error(which, 429))
    assert script.calls == [PRIMARY] * per_key + [SPARE] * per_key
    assert not items and p.api_key == PRIMARY


@BOTH
def test_other_errors_do_not_rotate(monkeypatch, which):
    p, script, _, items = _setup(monkeypatch, which, [_error(which, 500)], SPARE)
    with pytest.raises(Exception):
        _run(p)
    assert script.calls == [PRIMARY] and not items


def test_gemini_error_kinds():
    assert gp.key_error_kind(_gemini_error(429)) == base.KEY_RATE_LIMITED
    assert gp.key_error_kind(_gemini_error(401)) == base.KEY_REJECTED
    assert gp.key_error_kind(_gemini_error(400, GEMINI_BAD_KEY)) == base.KEY_REJECTED
    assert gp.key_error_kind(_gemini_error(400)) is None
    assert gp.key_error_kind(RuntimeError("429")) is None


def test_anthropic_error_kinds():
    assert ap.key_error_kind(_anthropic_error(401)) == base.KEY_REJECTED
    assert ap.key_error_kind(_anthropic_error(429)) == base.KEY_RATE_LIMITED
    assert ap.key_error_kind(_anthropic_error(500)) is None


@pytest.mark.parametrize("slug,name,article", [
    ("gemini", "Gemini", "a"), ("anthropic", "Anthropic", "an"), ("openai", "OpenAI", "an")])
def test_inbox_item_names_the_provider(monkeypatch, slug, name, article):
    seen = []

    class _DB:
        def close(self):
            pass

    import app.database as database
    from app.services import action_items
    monkeypatch.setattr(database, "SessionLocal", lambda: _DB())
    monkeypatch.setattr(action_items, "create",
                        lambda db, kind, title, **kw: seen.append(dict(kind=kind, title=title, **kw)))
    base.raise_key_rejected_item(slug, name, "sk-secret-abcd", has_spare=True)
    base.raise_key_rejected_item(slug, name, "short", has_spare=False)
    item = seen[0]
    assert item["kind"] == base.KEY_REJECTED_KIND
    assert item["title"] == f"{name} refused an API key"
    assert f"the {name} API key ending in abcd" in item["body"]
    assert f"Your other {name} keys" in item["body"]
    assert "sk-secret" not in item["body"] and "sk-secret" not in item["dedupe_key"]
    assert item["dedupe_key"].startswith(f"{base.KEY_REJECTED_KIND}:{slug}:")
    assert f"{article} {name} API key" in seen[1]["body"]
    assert f"AI features that use {name} will not work" in seen[1]["body"]
