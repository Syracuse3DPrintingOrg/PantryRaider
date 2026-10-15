"""The shared satellite forwarding helper (services/upstream.py forward/send).

Every satellite proxy (pending, audit, the action inbox, timers, the NeoKey
timer hop and the forwarding middleware) goes through upstream.forward() or
upstream.send(). These run them against a real httpx client on a mock
transport, so the assertions read the request as it would leave the device.
"""
import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from starlette.requests import Request  # noqa: E402

from app.config import APP_VERSION, settings  # noqa: E402
from app.services import upstream  # noqa: E402

BASE = "http://main.test:9284"


@pytest.fixture(autouse=True)
def sat(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "remote_server_url", BASE, raising=False)
    monkeypatch.setattr(settings, "upstream_api_key", "sat-key", raising=False)
    return monkeypatch


class _Wire:
    def __init__(self, status=200, body=b'{"ok": true}', headers=None):
        self.requests: list[httpx.Request] = []
        self.status = status
        self.body = body
        self.headers = headers if headers is not None else {
            "content-type": "application/json"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        return httpx.Response(self.status, content=self.body, headers=self.headers)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


class _Dead:
    async def request(self, *a, **k):
        raise httpx.ConnectError("down")


def _request(method, path="/pending/scan", query=b"", body=b"", content_type=None,
             body_reads=None):
    headers = [(b"host", b"satellite.test")]
    if content_type:
        headers.append((b"content-type", content_type.encode()))
    scope = {"type": "http", "method": method, "path": path, "query_string": query,
             "headers": headers, "scheme": "http", "server": ("satellite.test", 80),
             "client": ("127.0.0.1", 50000)}

    async def receive():
        if body_reads is not None:
            body_reads.append(1)
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _values(request, name):
    return [v for k, v in request.headers.multi_items() if k.lower() == name.lower()]


def test_forward_passes_method_query_body_and_content_type():
    wire = _Wire()
    req = _request("POST", query=b"a=1&b=two", body=b'{"x": 1}',
                   content_type="application/json")
    resp = asyncio.run(upstream.forward(wire.client(), req, BASE + "/pending/scan"))
    assert resp.status_code == 200
    assert resp.body == b'{"ok": true}'
    (sent,) = wire.requests
    assert sent.method == "POST"
    assert str(sent.url) == BASE + "/pending/scan?a=1&b=two"
    assert sent.content == b'{"x": 1}'
    assert _values(sent, "Content-Type") == ["application/json"]
    assert _values(sent, "X-API-Key") == ["sat-key"]
    assert _values(sent, "X-PR-Client") == ["satellite/" + APP_VERSION]


def test_forward_get_never_reads_a_body():
    wire = _Wire()
    reads: list = []
    req = _request("GET", body=b"ignored", body_reads=reads)
    asyncio.run(upstream.forward(wire.client(), req, BASE + "/pending/count"))
    assert reads == []
    (sent,) = wire.requests
    assert sent.content == b""
    assert _values(sent, "Content-Type") == []


def test_forward_head_never_reads_a_body():
    wire = _Wire()
    reads: list = []
    req = _request("HEAD", body_reads=reads)
    asyncio.run(upstream.forward(wire.client(), req, BASE + "/pending/count"))
    assert reads == []


def test_read_get_body_reads_and_sends_it():
    wire = _Wire()
    reads: list = []
    req = _request("GET", body=b"payload", body_reads=reads)
    asyncio.run(upstream.forward(wire.client(), req, BASE + "/x", read_get_body=True))
    assert reads
    assert wire.requests[0].content == b"payload"


def test_explicit_params_replace_the_query():
    wire = _Wire()
    req = _request("GET", query=b"a=1")
    asyncio.run(upstream.forward(wire.client(), req, BASE + "/gadgets/outputs",
                                 params={"a": "1", "on_manage": "0"}))
    assert str(wire.requests[0].url) == BASE + "/gadgets/outputs?a=1&on_manage=0"


def test_reply_is_mirrored_verbatim():
    wire = _Wire(status=404, body=b"<p>nope</p>",
                 headers={"content-type": "text/html; charset=utf-8"})
    resp = asyncio.run(upstream.forward(wire.client(), _request("GET"), BASE + "/x"))
    assert resp.status_code == 404
    assert resp.body == b"<p>nope</p>"
    assert resp.headers["content-type"] == "text/html; charset=utf-8"


def test_reply_without_a_content_type_is_json():
    wire = _Wire(headers={})
    resp = asyncio.run(upstream.forward(wire.client(), _request("GET"), BASE + "/x"))
    assert resp.headers["content-type"] == "application/json"


def test_unreachable_server_is_a_502():
    resp = asyncio.run(upstream.forward(_Dead(), _request("POST", body=b"{}"), BASE))
    assert resp.status_code == 502
    assert json.loads(resp.body) == {"detail": upstream.UNREACHABLE}


def test_unreachable_detail_can_be_chosen():
    resp = asyncio.run(upstream.forward(
        _Dead(), _request("GET"), BASE, unreachable_detail=upstream.UNREACHABLE_RETURN))
    assert json.loads(resp.body) == {"detail": upstream.UNREACHABLE_RETURN}


def test_send_json_lets_httpx_set_the_content_type_once():
    wire = _Wire()
    asyncio.run(upstream.send(wire.client(), "POST", BASE + "/timers",
                              json={"label": "Pasta", "seconds": 540.0}))
    (sent,) = wire.requests
    assert json.loads(sent.content) == {"label": "Pasta", "seconds": 540.0}
    assert _values(sent, "Content-Type") == ["application/json"]


def test_send_passes_only_the_given_keywords():
    seen = {}

    class Recorder:
        async def request(self, method, url, **kwargs):
            seen.update(kwargs)
            return httpx.Response(200, json={})

    asyncio.run(upstream.send(Recorder(), "POST", BASE, content_type="application/json",
                              content=b"{}"))
    assert set(seen) == {"headers", "content"}
    assert seen["headers"]["Content-Type"] == "application/json"


@pytest.mark.parametrize("path,ok", [
    ("/recipes/abc", True),
    ("/gadgets/../ha/connect", False),
    ("/gadgets/./state", False),
    ("/x\\y", False),
    ("/x\ny", False),
    ("relative", False),
])
def test_path_ok(path, ok):
    assert upstream.path_ok(path) is ok


def test_path_url_keeps_an_encoded_percent():
    assert upstream.path_url(BASE, "/recipes/50%") == BASE + "/recipes/50%25"
