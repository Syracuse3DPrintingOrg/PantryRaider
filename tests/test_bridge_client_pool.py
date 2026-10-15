"""The pooled host-bridge client and the bridge's keep-alive (FoodAssistant-e6k25).

bridge_client() used to build a new httpx.AsyncClient, and so a new TCP
connection, for every bridge call. It now hands out one shared client per
event loop, and the bridge answers over HTTP/1.1 so the connection is
actually kept. These run the real bridge handler class on a loopback port
(with a few test routes) and count the connections it accepts.

Run: python -m pytest tests/test_bridge_client_pool.py -q
"""
from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
import json
import socket
import threading
import time
import http.server
from pathlib import Path

import httpx
import pytest

from app.services import bridge as app_bridge

REPO = Path(__file__).resolve().parents[1]
BRIDGE = REPO / "scripts" / "image-build" / "foodassistant-host-bridge"


def _load_bridge():
    loader = importlib.machinery.SourceFileLoader("foodassistant_host_bridge_pool", str(BRIDGE))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


host_bridge = _load_bridge()


class _TestHandler(host_bridge._Handler):
    """The bridge's own handler (keep-alive plumbing, _send, _body) with a
    few routes that need no root and touch nothing on the host."""

    connections: list = []

    def setup(self):
        type(self).connections.append(self.client_address)
        super().setup()

    def _route(self):
        token = self.headers.get("X-Bridge-Token", "")
        if self.path == "/echo":
            body = self._body() if self.command == "POST" else {}
            self._send(200, {"token": token, "body": body})
        elif self.path == "/slow":
            time.sleep(0.6)
            self._send(200, {"ok": True})
        elif self.path == "/unread":
            # Replies without reading the request body.
            self._send(200, {"ok": True})
        elif self.path == "/silent":
            return
        else:
            self._send(404, {"ok": False})

    do_GET = _route
    do_POST = _route


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(app_bridge.settings, "data_dir", str(tmp_path))
    app_bridge.invalidate_bridge_token()
    _TestHandler.connections = []
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TestHandler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path
    httpd.shutdown()
    httpd.server_close()
    app_bridge.invalidate_bridge_token()
    asyncio.run(app_bridge.aclose_bridge_client())


def test_the_bridge_speaks_http_1_1():
    assert host_bridge._Handler.protocol_version == "HTTP/1.1"
    assert 0 < host_bridge._Handler.timeout


def test_sequential_calls_share_one_connection(server):
    base, _ = server

    async def run():
        for _ in range(3):
            async with app_bridge.bridge_client(timeout=5.0) as c:
                r = await c.post(f"{base}/echo", json={"n": 1})
                assert r.json()["body"] == {"n": 1}
        async with app_bridge.bridge_client(timeout=5.0) as c:
            assert (await c.get(f"{base}/echo")).status_code == 200

    asyncio.run(run())
    assert len(_TestHandler.connections) == 1


def test_leaving_the_block_does_not_close_the_shared_client(server):
    base, _ = server

    async def run():
        async with app_bridge.bridge_client(timeout=5.0) as c:
            await c.get(f"{base}/echo")
        shared = app_bridge._shared
        assert shared is not None and not shared.is_closed
        await app_bridge.aclose_bridge_client()
        assert shared.is_closed

    asyncio.run(run())


def test_each_call_gets_its_own_timeout(server):
    base, _ = server

    async def run():
        async with app_bridge.bridge_client(timeout=0.1) as c:
            with pytest.raises(httpx.ReadTimeout):
                await c.get(f"{base}/slow")
        async with app_bridge.bridge_client(timeout=5.0) as c:
            assert (await c.get(f"{base}/slow")).json() == {"ok": True}

    asyncio.run(run())


def test_a_changed_token_is_sent_on_the_same_connection(server):
    base, data_dir = server
    (data_dir / "bridge-token").write_text("first\n")

    async def run():
        async with app_bridge.bridge_client(timeout=5.0) as c:
            assert (await c.get(f"{base}/echo")).json()["token"] == "first"
        (data_dir / "bridge-token").write_text("second\n")
        app_bridge.invalidate_bridge_token()
        async with app_bridge.bridge_client(timeout=5.0) as c:
            assert (await c.get(f"{base}/echo")).json()["token"] == "second"

    asyncio.run(run())
    assert len(_TestHandler.connections) == 1


def test_a_caller_header_is_added_to_the_token(server):
    base, data_dir = server
    (data_dir / "bridge-token").write_text("tok\n")

    async def run():
        async with app_bridge.bridge_client(timeout=5.0) as c:
            r = await c.get(f"{base}/echo", headers={"X-Extra": "1"})
            assert r.json()["token"] == "tok"
            assert r.request.headers["X-Extra"] == "1"

    asyncio.run(run())


def test_a_new_event_loop_gets_a_fresh_client(server):
    base, _ = server
    seen = []

    async def run():
        async with app_bridge.bridge_client(timeout=5.0) as c:
            await c.get(f"{base}/echo")
        seen.append(app_bridge._shared)

    asyncio.run(run())
    asyncio.run(run())
    assert seen[0] is not seen[1]
    assert len(_TestHandler.connections) == 2


def test_a_closed_client_is_rebuilt(server):
    base, _ = server

    async def run():
        async with app_bridge.bridge_client(timeout=5.0) as c:
            await c.get(f"{base}/echo")
        first = app_bridge._shared
        await first.aclose()
        async with app_bridge.bridge_client(timeout=5.0) as c:
            assert (await c.get(f"{base}/echo")).status_code == 200
        assert app_bridge._shared is not first

    asyncio.run(run())


def test_a_401_still_drops_the_cached_token(server, monkeypatch):
    base, data_dir = server
    (data_dir / "bridge-token").write_text("stale\n")
    assert app_bridge.bridge_token() == "stale"
    (data_dir / "bridge-token").write_text("rotated\n")

    async def run():
        async with app_bridge.bridge_client(timeout=5.0) as c:
            # The test routes never 401, so drive the hook the way httpx does.
            hooks = c.event_hooks["response"]
            assert app_bridge._drop_token_on_401 in hooks
            req = httpx.Request("GET", f"{base}/echo")
            await hooks[0](httpx.Response(401, request=req))

    asyncio.run(run())
    assert app_bridge.bridge_token() == "rotated"


# --- the bridge side of keep-alive ------------------------------------------

def _raw_exchange(base: str, requests: list[bytes]) -> tuple[list[bytes], bool]:
    """Send raw requests on one socket; return each reply and whether the
    bridge closed the socket afterwards."""
    host, port = base.rsplit("/", 1)[1].split(":")
    replies = []
    with socket.create_connection((host, int(port)), timeout=3) as s:
        f = s.makefile("rb")
        for raw in requests:
            s.sendall(raw)
            status = f.readline()
            length = 0
            while True:
                line = f.readline()
                if line in (b"\r\n", b""):
                    break
                name, _, value = line.decode().partition(":")
                if name.lower() == "content-length":
                    length = int(value)
            replies.append(status + f.read(length))
        closed = f.read(1) == b""
    return replies, closed


def _post(path: str, body: bytes, close: bool = False) -> bytes:
    extra = b"Connection: close\r\n" if close else b""
    return (b"POST " + path.encode() + b" HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n" + extra + b"\r\n" + body)


def test_the_bridge_keeps_a_clean_connection_open(server):
    base, _ = server
    body = json.dumps({"a": 1}).encode()
    replies, closed = _raw_exchange(base, [_post("/echo", body), _post("/echo", body, close=True)])
    assert all(r.startswith(b"HTTP/1.1 200") for r in replies)
    assert len(_TestHandler.connections) == 1
    assert closed


def test_a_small_unread_body_is_drained_and_the_connection_kept(server):
    base, _ = server
    # A refused request whose body the handler never read: the bridge reads
    # and drops it before replying, so the next request starts clean.
    replies, closed = _raw_exchange(base, [_post("/unread", b'{"x": 1}'),
                                           _post("/echo", b'{"a": 1}', close=True)])
    assert all(r.startswith(b"HTTP/1.1 200") for r in replies)
    assert len(_TestHandler.connections) == 1
    assert closed


def test_a_big_unread_body_closes_and_says_so(server, monkeypatch):
    base, _ = server
    monkeypatch.setattr(_TestHandler, "_DRAIN_LIMIT", 4)
    host, port = base.rsplit("/", 1)[1].split(":")
    with socket.create_connection((host, int(port)), timeout=3) as s:
        s.sendall(_post("/unread", b'{"x": 1}'))
        reply = s.makefile("rb").read()   # until the bridge closes
    head = reply.split(b"\r\n\r\n", 1)[0].lower()
    assert head.startswith(b"http/1.1 200")
    assert b"connection: close" in head


def test_a_handler_that_never_replies_closes_rather_than_hangs(server):
    base, _ = server

    async def run():
        async with app_bridge.bridge_client(timeout=5.0) as c:
            started = time.monotonic()
            with pytest.raises(httpx.HTTPError):
                await c.get(f"{base}/silent")
            assert time.monotonic() - started < 2.0
            # The pool recovers on a fresh connection.
            assert (await c.get(f"{base}/echo")).status_code == 200

    asyncio.run(run())
