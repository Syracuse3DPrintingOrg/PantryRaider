"""Gemini calls share one upstream client per event loop.

A client per call rebuilt an SSL context (loading the CA bundle) and a fresh
connection pool on the event loop each time, which under load was most of
what held the loop up. Calls now reuse one client and its kept-alive
connections.
"""
import asyncio
import json

import httpx

from app import forwarder
from app.forwarder import GeminiForwarder


def _reply(request):
    return httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": json.dumps({"name": "Beans"})}]}}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
    })


def test_calls_on_one_loop_reuse_one_client(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return _reply(request)

    made = []
    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        made.append(kw)
        return real(*a, **kw)

    monkeypatch.setattr(forwarder.httpx, "AsyncClient", factory)
    monkeypatch.setattr(forwarder, "_shared", {"client": None, "loop": None})
    fwd = GeminiForwarder(api_key="k", timeout=7.0)

    async def three():
        return [await fwd.forward("enrich", None, "", "beans") for _ in range(3)]

    results = asyncio.run(three())
    assert [r.tokens for r in results] == [15, 15, 15]
    assert len(made) == 1          # one client for all three calls
    assert len(seen) == 3
    # The forwarder's timeout still applies per request.
    assert all(r.extensions["timeout"]["read"] == 7.0 for r in seen)

    # A new event loop gets a client of its own.
    asyncio.run(three())
    assert len(made) == 2


def test_an_injected_transport_is_used_as_before():
    calls = []

    def handler(request):
        calls.append(request)
        return _reply(request)

    fwd = GeminiForwarder(api_key="k", transport=httpx.MockTransport(handler))
    result = asyncio.run(fwd.forward("enrich", None, "", "beans"))
    assert result.tokens == 15 and len(calls) == 1
