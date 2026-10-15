"""The remaining connection tests never fetch this device or the metadata address.

/setup/test/remote, the Ollama branch of /setup/test/provider and the Immich
album fetch took a caller-supplied address straight to a plain httpx client,
unlike the Grocy and Mealie tests (security review, Oct 2026).
"""
import asyncio
import sys
from pathlib import Path

import httpx
import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.routers.setup import connections, deployment  # noqa: E402
from app.routers.setup.deployment import TestRemotePayload as RemotePayload  # noqa: E402
from app.routers.setup.page import TestProviderPayload as ProviderPayload  # noqa: E402
from app.services import egress, photo_source  # noqa: E402


@pytest.fixture
def no_plain_fetch(monkeypatch):
    """Fail the test if a plain client is opened, and record guarded ones."""
    guarded = []

    def _plain(*a, **k):
        raise AssertionError("a plain httpx client was opened")

    class _Refusing:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            raise httpx.ConnectError("refused by the guard")

    def _guarded(**kw):
        guarded.append(kw)
        return _Refusing()

    monkeypatch.setattr(deployment.httpx, "AsyncClient", _plain)
    monkeypatch.setattr(egress, "guarded_async_client", _guarded)
    return guarded


@pytest.mark.parametrize("url", ["http://127.0.0.1:9284", "http://169.254.169.254"])
def test_remote_test_refuses_this_device_and_metadata(no_plain_fetch, url):
    out = asyncio.run(deployment.test_remote(
        RemotePayload(remote_server_url=url)))
    assert out["ok"] is False and "not allowed" in out["error"]
    assert no_plain_fetch == []


def test_remote_test_uses_the_guarded_client(monkeypatch):
    seen = []

    class _Fake:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            seen.append(url)
            return httpx.Response(200)

    monkeypatch.setattr(deployment, "_refuse_probe_target", lambda url: "")
    monkeypatch.setattr(egress, "guarded_async_client",
                        lambda **kw: seen.append(kw) or _Fake())
    out = asyncio.run(deployment.test_remote(
        RemotePayload(remote_server_url="http://server.lan:9284")))
    assert out["ok"] is True
    assert seen[0]["allow_private"] is True and seen[1] == "http://server.lan:9284/health"


def test_ollama_test_refuses_an_entered_loopback(no_plain_fetch, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "server")
    out = asyncio.run(connections.test_provider(ProviderPayload(
        provider="ollama", model="llava:7b", base_url="http://127.0.0.1:9284")))
    assert out["ok"] is False and "not allowed" in out["error"]
    assert no_plain_fetch == []


def test_immich_album_fetch_is_guarded(no_plain_fetch):
    photo_source.invalidate_immich_cache()
    ids = asyncio.run(photo_source.immich_album_asset_ids(
        "http://127.0.0.1:2283", "key", "0b6e9b5a-1c55-4c2e-9d1a-3f0a6a7b8c9d"))
    assert ids == []
    assert no_plain_fetch and no_plain_fetch[0]["allow_private"] is True
