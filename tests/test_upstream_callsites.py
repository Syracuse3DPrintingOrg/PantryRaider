"""Every satellite-to-server call goes through services/upstream.py.

A satellite (pi_remote) forwards pending scans, the audit, the action inbox,
timers, the NeoKey mode read, gadget pushes and the shopping list to its main
server. Each of those requests is built with upstream.request_kwargs(), so it
carries X-PR-Client and X-PR-Caps (relay1) next to the X-API-Key it always
sent, and each "is this satellite linked" test reads settings.upstream_linked().
Apart from the two new headers the request is unchanged: same method, URL,
query, body and a caller's Content-Type sent exactly once.

The router sites run against a real httpx client on a mock transport, so the
assertions read the request as it would leave the device. The last test keeps
it that way: no module outside the allowlist reads settings.upstream_api_key.
"""
import asyncio
import json
import re
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "service"
sys.path.insert(0, str(SERVICE))

from starlette.requests import Request  # noqa: E402

from app.config import APP_VERSION, settings  # noqa: E402
from app.integrations.shopping import GrocyShoppingBackend  # noqa: E402
from app.routers import action_items as action_items_router  # noqa: E402
from app.routers import audit as audit_router  # noqa: E402
from app.routers import current_recipe as current_recipe_router  # noqa: E402
from app.routers import pending as pending_router  # noqa: E402
from app.services import gadgets_relay, stemma  # noqa: E402

BASE = "http://main.test:9284"


class _Wire:
    """A mock transport that records every request and answers like a server."""

    def __init__(self, reply=None):
        self.requests: list[httpx.Request] = []
        self.reply = reply if reply is not None else {"ok": True, "from": "main-server"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        return httpx.Response(200, json=self.reply)

    def async_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def sat(monkeypatch, tmp_path):
    """A linked satellite holding only the shared upstream key."""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "remote_server_url", BASE, raising=False)
    monkeypatch.setattr(settings, "upstream_api_key", "sat-key", raising=False)
    monkeypatch.setattr(settings, "upstream_credential_id", "", raising=False)
    monkeypatch.setattr(settings, "relay_gadgets_upstream", True, raising=False)
    stemma._mode_cache.invalidate()
    current_recipe_router._timers_cache.invalidate()
    yield monkeypatch
    stemma._mode_cache.invalidate()
    current_recipe_router._timers_cache.invalidate()


@pytest.fixture
def unlinked(sat):
    """The same satellite with neither a key nor a credential of its own."""
    sat.setattr(settings, "upstream_api_key", "", raising=False)
    sat.setattr(settings, "upstream_credential_id", "", raising=False)
    return sat


def _wire_into(monkeypatch, module, reply=None) -> _Wire:
    wire = _Wire(reply)
    monkeypatch.setattr(module, "_fwd_client", wire.async_client())
    return wire


def _browser_request(method: str, path: str, query: bytes = b"", body: bytes = b"",
                     content_type: str | None = None) -> Request:
    headers = [(b"host", b"satellite.test")]
    if content_type:
        headers.append((b"content-type", content_type.encode()))
    scope = {"type": "http", "method": method, "path": path, "query_string": query,
             "headers": headers, "scheme": "http", "server": ("satellite.test", 80),
             "client": ("127.0.0.1", 50000)}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _header_values(request: httpx.Request, name: str) -> list[str]:
    return [v for k, v in request.headers.multi_items() if k.lower() == name.lower()]


def _assert_identified(request: httpx.Request, key: str | None = "sat-key") -> None:
    assert _header_values(request, "X-PR-Client") == ["satellite/" + APP_VERSION]
    caps = _header_values(request, "X-PR-Caps")
    assert len(caps) == 1 and "relay1" in caps[0].split(",")
    if key is None:
        assert _header_values(request, "X-API-Key") == []
    else:
        assert _header_values(request, "X-API-Key") == [key]


# -- The router forwards ------------------------------------------------------------

_FORWARDS = [
    # (module, the call, the URL the server sees)
    (pending_router, lambda r: pending_router._forward(r, "/scan"), BASE + "/pending/scan"),
    (audit_router, lambda r: audit_router._forward(r, "/scan"), BASE + "/audit/scan"),
    (action_items_router, lambda r: action_items_router._forward(r, "/7/snooze"),
     BASE + "/action-items/7/snooze"),
    (current_recipe_router, lambda r: current_recipe_router._forward(r, "/timers/abc"),
     BASE + "/timers/abc"),
]
_FORWARD_IDS = ["pending", "audit", "action_items", "current_recipe"]


@pytest.mark.parametrize("module,call,url", _FORWARDS, ids=_FORWARD_IDS)
def test_a_forwarded_post_is_unchanged_apart_from_identification(sat, module, call, url):
    wire = _wire_into(sat, module)
    body = b'{"barcode": "078000035483", "qty": 2}'
    req = _browser_request("POST", "/x", query=b"location=2&dry=1", body=body,
                           content_type="application/json; charset=utf-8")
    resp = asyncio.run(call(req))
    assert resp.status_code == 200
    assert json.loads(resp.body) == {"ok": True, "from": "main-server"}
    (sent,) = wire.requests
    assert sent.method == "POST"
    assert str(sent.url) == url + "?location=2&dry=1"
    assert dict(sent.url.params) == {"location": "2", "dry": "1"}
    assert sent.content == body
    _assert_identified(sent)
    assert _header_values(sent, "Content-Type") == ["application/json; charset=utf-8"]


@pytest.mark.parametrize("module,call,url", _FORWARDS, ids=_FORWARD_IDS)
def test_a_forwarded_get_carries_no_body_and_no_content_type(sat, module, call, url):
    wire = _wire_into(sat, module)
    resp = asyncio.run(call(_browser_request("GET", "/x")))
    assert resp.status_code == 200
    (sent,) = wire.requests
    assert sent.method == "GET"
    assert str(sent.url) == url
    assert sent.content == b""
    _assert_identified(sent)
    assert _header_values(sent, "Content-Type") == []


@pytest.mark.parametrize("module,call,url", _FORWARDS, ids=_FORWARD_IDS)
def test_a_credential_alone_links_the_forward(sat, module, call, url):
    """A device credential with no shared key still counts as linked; the
    request then simply has no X-API-Key to send."""
    sat.setattr(settings, "upstream_api_key", "", raising=False)
    sat.setattr(settings, "upstream_credential_id", "cred-1", raising=False)
    assert module._upstream() == BASE
    wire = _wire_into(sat, module)
    asyncio.run(call(_browser_request("GET", "/x")))
    (sent,) = wire.requests
    _assert_identified(sent, key=None)


@pytest.mark.parametrize("module", [pending_router, audit_router, action_items_router,
                                    current_recipe_router], ids=_FORWARD_IDS)
def test_an_unlinked_satellite_does_not_forward(unlinked, module):
    assert module._upstream() is None


@pytest.mark.parametrize("module", [pending_router, audit_router, action_items_router,
                                    current_recipe_router], ids=_FORWARD_IDS)
def test_a_server_never_forwards(sat, module):
    sat.setattr(settings, "deployment_mode", "server", raising=False)
    assert module._upstream() is None


def test_scanner_mode_forward(sat):
    wire = _wire_into(sat, pending_router, reply={"mode": "consume", "label": "Use"})
    resp = asyncio.run(pending_router._forward_scanner_mode("consume"))
    assert resp.status_code == 200
    (sent,) = wire.requests
    assert sent.method == "POST"
    assert str(sent.url) == BASE + "/pending/scanner-mode"
    assert sent.content == json.dumps({"mode": "consume"}).encode()
    _assert_identified(sent)
    assert _header_values(sent, "Content-Type") == ["application/json"]


def test_timer_create_forward(sat):
    wire = _wire_into(sat, current_recipe_router, reply={"id": "t1"})
    resp = asyncio.run(current_recipe_router._create_timer_upstream("Pasta", 540.0))
    assert resp.status_code == 200
    (sent,) = wire.requests
    assert sent.method == "POST"
    assert str(sent.url) == BASE + "/timers"
    assert json.loads(sent.content) == {"label": "Pasta", "seconds": 540.0}
    _assert_identified(sent)
    # httpx sets the JSON Content-Type itself; it must not be doubled.
    assert _header_values(sent, "Content-Type") == ["application/json"]


# -- The NeoKey mode read (services/stemma.py) ---------------------------------------

def _wire_async_clients(monkeypatch, wire: _Wire) -> None:
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(wire.handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def test_stemma_reads_the_mode_from_the_server(sat):
    wire = _Wire(reply={"mode": "consume", "label": "Use"})
    _wire_async_clients(sat, wire)
    state = asyncio.run(stemma.current_mode_state())
    assert state["mode"] == "consume"
    (sent,) = wire.requests
    assert sent.method == "GET"
    assert str(sent.url) == BASE + "/pending/scanner-mode"
    assert sent.content == b""
    _assert_identified(sent)
    assert _header_values(sent, "Content-Type") == []


def test_stemma_unlinked_reads_the_local_mode(unlinked):
    wire = _Wire(reply={"mode": "consume"})
    _wire_async_clients(unlinked, wire)
    unlinked.setattr(stemma, "scanner_mode_state", lambda: {"mode": "inventory"})
    assert asyncio.run(stemma.current_mode_state()) == {"mode": "inventory"}
    assert wire.requests == []


# -- Gadget pushes (services/gadgets_relay.py) ---------------------------------------

def _wire_httpx_post(monkeypatch, wire: _Wire) -> None:
    client = wire.client()
    monkeypatch.setattr(gadgets_relay.httpx, "post",
                        lambda url, **kwargs: client.post(url, **kwargs))


def test_gadget_push_forward(sat):
    wire = _Wire(reply={"ok": True})
    _wire_httpx_post(sat, wire)
    item = {"source": "bandit", "devices": [{"id": "aa", "temp_c": 4.0}], "discovered": []}
    assert gadgets_relay.relay_active() is True
    assert gadgets_relay._post(item) is True
    (sent,) = wire.requests
    assert sent.method == "POST"
    assert str(sent.url) == BASE + "/gadgets/readings"
    assert json.loads(sent.content) == item
    _assert_identified(sent)
    assert _header_values(sent, "Content-Type") == ["application/json"]


def test_gadget_push_unlinked_sends_nothing(unlinked):
    wire = _Wire()
    _wire_httpx_post(unlinked, wire)
    assert gadgets_relay.relay_active() is False
    assert gadgets_relay._post({"devices": [{"id": "aa"}], "discovered": []}) is False
    assert wire.requests == []


def test_a_credential_alone_links_the_mode_read_and_the_gadget_push(sat):
    """The two service sites follow upstream_linked() too: a device credential
    with no shared key forwards, with no X-API-Key on the wire."""
    sat.setattr(settings, "upstream_api_key", "", raising=False)
    sat.setattr(settings, "upstream_credential_id", "cred-1", raising=False)
    wire = _Wire(reply={"mode": "consume", "label": "Use", "ok": True})
    _wire_async_clients(sat, wire)
    _wire_httpx_post(sat, wire)
    assert asyncio.run(stemma.current_mode_state())["mode"] == "consume"
    assert gadgets_relay.relay_active() is True
    assert gadgets_relay._post({"devices": [{"id": "aa"}], "discovered": []}) is True
    assert [str(r.url) for r in wire.requests] == [
        BASE + "/pending/scanner-mode", BASE + "/gadgets/readings"]
    for sent in wire.requests:
        _assert_identified(sent, key=None)


def test_gadget_push_without_a_server_sends_nothing(sat):
    sat.setattr(settings, "remote_server_url", "", raising=False)
    wire = _Wire()
    _wire_httpx_post(sat, wire)
    assert gadgets_relay._post({"devices": [{"id": "aa"}], "discovered": []}) is False
    assert wire.requests == []


# -- The shopping backend (integrations/shopping.py) ---------------------------------

def test_shopping_backend_follows_the_link(sat):
    backend = GrocyShoppingBackend()
    assert backend.configured() is True
    sat.setattr(settings, "upstream_api_key", "", raising=False)
    sat.setattr(settings, "upstream_credential_id", "cred-1", raising=False)
    assert backend.configured() is True
    sat.setattr(settings, "upstream_credential_id", "", raising=False)
    assert backend.configured() is False


# -- Nothing else reads the shared key directly --------------------------------------

_KEY_READ = re.compile(r"(?<![A-Za-z0-9_])(?:settings|_s)[.]upstream_api_key(?![A-Za-z0-9_])")
# config.py defines the setting, services/upstream.py turns it into headers and
# the pull's signing key, and the setup routes (routers/setup/deployment.py)
# fall back to it when the switch to a satellite leaves the key field blank.
_ALLOWED = {"config.py", "services/upstream.py", "routers/setup/deployment.py"}
# The same read spelled through getattr, so the rule cannot be dodged by it.
_KEY_GETATTR = re.compile(
    r"getattr\(\s*(?:settings|_s)\s*,\s*[\"']upstream_api_key[\"']")


def test_the_key_pattern_is_not_vacuous():
    assert _KEY_READ.search("k = settings.upstream_api_key")
    assert _KEY_READ.search("if _s.upstream_api_key:")
    assert not _KEY_READ.search("settings.upstream_linked()")
    assert not _KEY_READ.search("settings.upstream_api_key_hint")
    assert not _KEY_READ.search('fields = ("upstream_api_key",)')
    assert _KEY_GETATTR.search('key = getattr(settings, "upstream_api_key", "")')
    assert _KEY_GETATTR.search("getattr(_s, 'upstream_api_key')")
    assert not _KEY_GETATTR.search('getattr(settings, "upstream_api_key_hint")')


def test_only_the_upstream_module_reads_the_shared_key():
    app_dir = SERVICE / "app"
    files = sorted(app_dir.rglob("*.py"))
    assert len(files) >= 150, len(files)
    offenders = []
    allowed_hits = 0
    for path in files:
        rel = path.relative_to(app_dir).as_posix()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not (_KEY_READ.search(line) or _KEY_GETATTR.search(line)):
                continue
            if rel in _ALLOWED:
                allowed_hits += 1
            else:
                offenders.append(f"service/app/{rel}:{lineno}: {line.strip()}")
    # The allowlisted readers still exist, so a renamed setting cannot make
    # this test pass by matching nothing.
    assert allowed_hits >= 1
    assert not offenders, (
        "Use settings.upstream_linked() and upstream.request_kwargs() instead "
        "of reading settings.upstream_api_key:\n" + "\n".join(offenders))
