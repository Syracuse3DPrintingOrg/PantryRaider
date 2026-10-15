"""Short-lived outbound clients share one TLS context (services/tls.py).

httpx loads the CA bundle into a new SSL context for every client, about 10 ms
on a desktop and far more on a Pi, and the per-request clients here are built
on the event loop. These pin that the hot ones reuse the shared context.
"""
from __future__ import annotations

import asyncio
import ssl

import httpx
import pytest

from app.services import bridge, egress, tls


def test_the_context_is_built_once_and_verifies():
    a = tls.default_context()
    assert a is tls.default_context()
    assert isinstance(a, ssl.SSLContext)
    assert a.verify_mode == ssl.CERT_REQUIRED and a.check_hostname


def test_verify_keeps_an_explicit_choice():
    assert tls.verify(True) is tls.default_context()
    assert tls.verify(False) is False
    own = ssl.create_default_context()
    assert tls.verify(own) is own


async def _open_bridge_client():
    # The bridge client is pooled and built on first use inside a loop.
    await bridge.aclose_bridge_client()
    async with bridge.bridge_client(timeout=3):
        pass
    await bridge.aclose_bridge_client()


def _built_contexts(monkeypatch):
    calls = []
    real = httpx._transports.default.create_ssl_context

    def counting(*a, **kw):
        calls.append(kw.get("verify", a[0] if a else True))
        return real(*a, **kw)

    monkeypatch.setattr(httpx._transports.default, "create_ssl_context", counting)
    return calls


@pytest.mark.parametrize("make", [
    lambda: egress.guarded_async_client(allow_private=True, timeout=3),
    lambda: egress.guarded_client(timeout=3),
    lambda: asyncio.run(_open_bridge_client()),
])
def test_hot_clients_reuse_the_shared_context(monkeypatch, make):
    tls.default_context()
    calls = _built_contexts(monkeypatch)
    make()
    assert calls, "the transport should have been handed a verify value"
    assert all(v is tls.default_context() for v in calls)


def test_an_unverified_client_stays_unverified(monkeypatch):
    calls = _built_contexts(monkeypatch)
    egress.guarded_async_client(verify=False)
    assert calls == [False]
