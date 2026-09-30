"""Satellite side of the proxy policy, and the update-channel push.

A satellite's Grocy and Mealie clients name their release to the main server
(X-PR-Client), and a path refusal from the server's proxy reaches the kiosk as
the server's own words instead of a bare "Grocy 403". A plain Grocy or Mealie
403 is left exactly as before.

The update-channel push to the host bridge is token-gated on the bridge side,
so it now carries the bridge token and drops a stale one on a 401, like the
neighbouring timezone and Stream Deck pushes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings, APP_VERSION  # noqa: E402
from app.services import grocy as grocy_svc  # noqa: E402
from app.services import mealie as mealie_svc  # noqa: E402
from app.services import satellite as sat  # noqa: E402
from app.services.grocy import GrocyClient, GrocyError  # noqa: E402
from app.services.mealie import MealieClient, MealieError  # noqa: E402


class _Recorder:
    """Stands in for a shared httpx.AsyncClient: one fixed answer, calls kept."""

    def __init__(self, status=200, body="[]", content_type="application/json"):
        self.status, self.body, self.content_type = status, body, content_type
        self.calls = []

    async def request(self, method, url, headers=None, json=None, params=None):
        self.calls.append({"method": method, "url": url, "headers": headers or {}})
        return httpx.Response(self.status, content=self.body.encode(),
                              headers={"content-type": self.content_type},
                              request=httpx.Request(method, url))


@pytest.fixture
def satellite(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "remote_server_url", "http://server.test:9284", raising=False)
    monkeypatch.setattr(settings, "upstream_api_key", "shared-key", raising=False)
    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie:9000", raising=False)
    # A satellite paired after the server stopped handing out backend keys.
    monkeypatch.setattr(settings, "grocy_api_key", "", raising=False)
    monkeypatch.setattr(settings, "mealie_api_key", "", raising=False)


def _install(monkeypatch, module, **kw):
    rec = _Recorder(**kw)
    monkeypatch.setattr(module, "_client", rec)
    return rec


_REFUSAL = json.dumps({
    "detail": ("The main server does not pass this Grocy request along "
               "(GET /api/stock). Update this device and the main server to "
               "the same version."),
    "code": "proxy_path_not_allowed"})


# --- client header -----------------------------------------------------------

@pytest.mark.anyio
async def test_satellite_grocy_client_names_its_release(satellite, monkeypatch):
    rec = _install(monkeypatch, grocy_svc)
    assert await GrocyClient().get_stock() == []
    call = rec.calls[0]
    assert call["url"] == "http://server.test:9284/api/proxy/grocy/api/stock"
    assert call["headers"]["X-PR-Client"] == f"satellite/{APP_VERSION}"
    assert call["headers"]["X-API-Key"] == "shared-key"
    assert "GROCY-API-KEY" not in call["headers"]  # the key never leaves the server


@pytest.mark.anyio
async def test_satellite_mealie_client_names_its_release(satellite, monkeypatch):
    rec = _install(monkeypatch, mealie_svc, body="{}")
    m = MealieClient()
    assert m.configured  # works without a Mealie key on the satellite
    await m._request("GET", "/users/self")
    call = rec.calls[0]
    assert call["url"] == "http://server.test:9284/api/proxy/mealie/api/users/self"
    assert call["headers"]["X-PR-Client"] == f"satellite/{APP_VERSION}"
    assert "Authorization" not in call["headers"]


def test_server_clients_do_not_send_the_client_header(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    assert "X-PR-Client" not in GrocyClient().headers
    assert "X-PR-Client" not in MealieClient().headers


# --- refusal mapping ---------------------------------------------------------

@pytest.mark.anyio
async def test_grocy_refusal_becomes_the_servers_words(satellite, monkeypatch):
    _install(monkeypatch, grocy_svc, status=403, body=_REFUSAL)
    with pytest.raises(GrocyError) as info:
        await GrocyClient().get_stock()
    assert info.value.kind == "config"
    assert str(info.value).startswith("The main server does not pass this Grocy request")
    assert "403" not in str(info.value)


@pytest.mark.anyio
async def test_grocy_denied_and_invalid_codes_map_too(satellite, monkeypatch):
    for status, code in ((403, "proxy_path_denied"), (400, "proxy_path_invalid")):
        _install(monkeypatch, grocy_svc, status=status,
                 body=json.dumps({"detail": "Refused here.", "code": code}))
        with pytest.raises(GrocyError) as info:
            await GrocyClient().get_stock()
        assert str(info.value) == "Refused here." and info.value.kind == "config"


@pytest.mark.anyio
async def test_plain_grocy_403_is_unchanged(satellite, monkeypatch):
    _install(monkeypatch, grocy_svc, status=403,
             body='{"error_message": "Permission denied"}')
    with pytest.raises(GrocyError) as info:
        await GrocyClient().get_stock()
    assert str(info.value).startswith("Grocy 403 on /stock")
    assert info.value.kind == "http"


@pytest.mark.anyio
async def test_mealie_refusal_becomes_the_servers_words(satellite, monkeypatch):
    _install(monkeypatch, mealie_svc, status=403, body=json.dumps(
        {"detail": "The main server does not pass this Mealie request along.",
         "code": "proxy_path_not_allowed"}))
    with pytest.raises(MealieError) as info:
        await MealieClient()._request("GET", "/recipes")
    assert str(info.value) == "The main server does not pass this Mealie request along."


@pytest.mark.anyio
async def test_plain_mealie_403_is_unchanged(satellite, monkeypatch):
    _install(monkeypatch, mealie_svc, status=403, body='{"detail": "Not allowed"}')
    with pytest.raises(MealieError) as info:
        await MealieClient()._request("GET", "/recipes")
    assert str(info.value).startswith("Mealie 403 on /recipes")


# --- update-channel push -------------------------------------------------------

class _Resp:
    def __init__(self, status):
        self.status_code = status


def test_update_channel_push_sends_bridge_token(monkeypatch):
    monkeypatch.setattr("app.hardware.is_raspberry_pi", lambda: True)
    monkeypatch.setattr(sat, "bridge_headers", lambda: {"X-Bridge-Token": "tok"})
    seen = {}

    def _post(url, json=None, timeout=None, headers=None):
        seen.update(url=url, json=json, headers=headers)
        return _Resp(200)
    monkeypatch.setattr(sat.httpx, "post", _post)
    assert sat._push_update_channel("stable") is True
    assert seen["url"].endswith("/update/channel")
    assert seen["json"] == {"channel": "stable"}
    assert seen["headers"] == {"X-Bridge-Token": "tok"}


def test_update_channel_push_drops_a_stale_token_on_401(monkeypatch):
    monkeypatch.setattr("app.hardware.is_raspberry_pi", lambda: True)
    monkeypatch.setattr(sat, "bridge_headers", lambda: {"X-Bridge-Token": "old"})
    dropped = []
    monkeypatch.setattr(sat, "invalidate_bridge_token", lambda: dropped.append(True))
    monkeypatch.setattr(sat.httpx, "post", lambda *a, **k: _Resp(401))
    assert sat._push_update_channel("main") is False
    assert dropped == [True]


def test_update_channel_push_old_bridge_404_is_quiet(monkeypatch):
    # An older bridge without the route: no token drop, just a False.
    monkeypatch.setattr("app.hardware.is_raspberry_pi", lambda: True)
    monkeypatch.setattr(sat, "bridge_headers", lambda: {})
    dropped = []
    monkeypatch.setattr(sat, "invalidate_bridge_token", lambda: dropped.append(True))
    monkeypatch.setattr(sat.httpx, "post", lambda *a, **k: _Resp(404))
    assert sat._push_update_channel("main") is False
    assert dropped == []


def test_recipe_printing_reaches_mealie_on_a_satellite_with_no_key(satellite, monkeypatch):
    """Printing a Mealie recipe on a satellite goes through the main server.

    A satellite paired after the server stopped sending backend keys holds no
    Mealie key, so the print route must ask the Mealie client whether it can
    reach Mealie instead of checking for a key of its own.
    """
    import asyncio
    from app.routers import printing as printing_router
    from app.services import recipe_source

    monkeypatch.setattr(recipe_source, "active_backend",
                        lambda: recipe_source.BACKEND_MEALIE)
    rec = _install(monkeypatch, mealie_svc, body=json.dumps({"slug": "soup", "name": "Soup"}))
    detail = asyncio.run(printing_router._mealie_get_recipe("soup"))
    assert detail and detail.get("slug") == "soup"
    assert rec.calls and rec.calls[0]["url"].startswith(
        "http://server.test:9284/api/proxy/mealie/api/recipes/soup")


def test_recipe_printing_skips_mealie_on_a_server_without_it(monkeypatch):
    import asyncio
    from app.routers import printing as printing_router
    from app.services import recipe_source

    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "mealie_api_key", "", raising=False)
    monkeypatch.setattr(recipe_source, "active_backend",
                        lambda: recipe_source.BACKEND_MEALIE)
    rec = _install(monkeypatch, mealie_svc)
    assert asyncio.run(printing_router._mealie_get_recipe("soup")) is None
    assert rec.calls == []
