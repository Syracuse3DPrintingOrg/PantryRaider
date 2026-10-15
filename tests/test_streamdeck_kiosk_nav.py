"""The Stream Deck's client for the kiosk launcher's navigation socket.

Covers the nav path rule the launcher copies byte for byte, the deck's path
conversion, and one request/reply over a real Unix socket on a temp path. No
browser, launcher or deck needed.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import socket
import tempfile
import time

import pytest

from foodassistant_streamdeck import kiosk_nav

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"),
                                reason="Unix sockets are not available here")


@pytest.fixture
def sock_dir():
    # A short directory: a Unix socket path must stay under about 100 bytes,
    # and pytest's tmp_path can be longer than that.
    d = tempfile.mkdtemp(prefix="pr-nav-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


# -- the nav path rule -------------------------------------------------------


@pytest.mark.parametrize("path", [
    "/ui/add",
    "/ui/camera?cam=Front%20Door",
    "/",
    "/" + "a" * (kiosk_nav.NAV_PATH_MAX - 1),
])
def test_valid_nav_path_accepts_app_paths(path):
    assert kiosk_nav.valid_nav_path(path) is True


@pytest.mark.parametrize("path", [
    "ui/add",                      # no leading slash
    "//evil.example/x",            # another host
    "/ui\\add",                    # backslash
    "/ui/add\n",                   # control character
    "/ui/\x00add",
    "/ui/\x7fadd",
    "",
    "/" + "a" * kiosk_nav.NAV_PATH_MAX,   # one over the limit
    None,
    b"/ui/add",
    42,
])
def test_valid_nav_path_refuses_everything_else(path):
    assert kiosk_nav.valid_nav_path(path) is False


def test_nav_path_max_is_2048():
    assert kiosk_nav.NAV_PATH_MAX == 2048


@pytest.mark.parametrize("raw,expected", [
    ("ui/add", "/ui/add"),
    ("/ui/add", "/ui/add"),
    ("//evil.example", "/evil.example"),
    ("ui/camera?cam=Front%20Door", "/ui/camera?cam=Front%20Door"),
    ("", "/"),
    (None, "/"),
])
def test_to_nav_path(raw, expected):
    assert kiosk_nav.to_nav_path(raw) == expected
    assert kiosk_nav.valid_nav_path(kiosk_nav.to_nav_path(raw))


def test_rule_block_is_self_contained():
    # The launcher copies the lines between these markers byte for byte, so
    # the block must define everything it uses.
    src = inspect.getsource(kiosk_nav)
    start = src.index("# --- nav path rule (shared with the Stream Deck; keep byte-identical) ---")
    end = src.index("# --- end nav path rule ---")
    block = src[start:end]
    ns: dict = {}
    exec(compile(block, "nav_rule", "exec"), ns)  # noqa: S102 - our own source
    assert ns["NAV_PATH_MAX"] == 2048
    assert ns["valid_nav_path"]("/ui/add") is True
    assert ns["valid_nav_path"]("ui/add") is False


def test_default_socket_path():
    assert kiosk_nav.DEFAULT_SOCKET == "/run/foodassistant-kiosk/nav.sock"


# -- request / navigate / ping over a real socket ----------------------------


def _serve(path, handler):
    """Run a one-line-in, one-line-out Unix server while ``handler`` decides
    each reply (a dict, or None to never answer). Returns (start, seen)."""
    seen: list = []

    async def on_conn(reader, writer):
        line = await reader.readline()
        msg = json.loads(line.decode("utf-8"))
        seen.append(msg)
        reply = handler(msg)
        if reply is None:
            await asyncio.sleep(30)
            return
        writer.write(json.dumps(reply).encode("utf-8") + b"\n")
        await writer.drain()
        writer.close()

    async def start():
        return await asyncio.start_unix_server(on_conn, path=path)

    return start, seen


def _launcher_like(msg):
    if msg.get("op") == "ping":
        return {"ok": True, "version": 1}
    if msg.get("op") == "navigate":
        if kiosk_nav.valid_nav_path(msg.get("path")):
            return {"ok": True}
        return {"ok": False, "error": "bad_path"}
    return {"ok": False, "error": "bad_request"}


def test_request_round_trip(sock_dir):
    path = os.path.join(sock_dir, "nav.sock")
    start, seen = _serve(path, _launcher_like)

    async def go():
        server = await start()
        try:
            reply = await kiosk_nav.request({"op": "ping"}, path)
            nav = await kiosk_nav.navigate("/ui/add", path)
            bad = await kiosk_nav.navigate("ui/add", path)
            pong = await kiosk_nav.ping(path)
        finally:
            server.close()
            await server.wait_closed()
        return reply, nav, bad, pong

    reply, nav, bad, pong = asyncio.run(go())
    assert reply == {"ok": True, "version": 1}
    assert nav == (True, "")
    # The path goes over as given; the launcher refuses one with no slash.
    assert bad == (False, "bad_path")
    assert pong is True
    assert seen[1] == {"op": "navigate", "path": "/ui/add"}
    assert seen[2] == {"op": "navigate", "path": "ui/add"}


def test_ping_needs_version_1(sock_dir):
    path = os.path.join(sock_dir, "nav.sock")
    start, _seen = _serve(path, lambda msg: {"ok": True, "version": 2})

    async def go():
        server = await start()
        try:
            return await kiosk_nav.ping(path)
        finally:
            server.close()
            await server.wait_closed()

    assert asyncio.run(go()) is False


def test_request_times_out_on_a_silent_server(sock_dir):
    path = os.path.join(sock_dir, "nav.sock")
    start, seen = _serve(path, lambda msg: None)

    async def go():
        server = await start()
        try:
            t0 = time.monotonic()
            reply = await kiosk_nav.request({"op": "ping"}, path, timeout=0.3)
            return reply, time.monotonic() - t0
        finally:
            server.close()

    reply, took = asyncio.run(go())
    assert reply is None
    assert took < 2.0
    assert seen == [{"op": "ping"}]


def test_default_timeout_is_three_seconds():
    for fn in (kiosk_nav.request, kiosk_nav.navigate, kiosk_nav.ping):
        assert inspect.signature(fn).parameters["timeout"].default == 3.0


def test_request_none_on_refused_connection(sock_dir):
    # A socket file nobody listens on refuses the connection.
    path = os.path.join(sock_dir, "nav.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    try:
        assert asyncio.run(kiosk_nav.request({"op": "ping"}, path)) is None
        assert asyncio.run(kiosk_nav.navigate("/ui/add", path)) == (False, "no_reply")
    finally:
        s.close()


def test_request_none_when_socket_missing(sock_dir):
    path = os.path.join(sock_dir, "missing.sock")
    assert asyncio.run(kiosk_nav.request({"op": "ping"}, path)) is None
    assert asyncio.run(kiosk_nav.ping(path)) is False


def test_request_none_on_a_garbage_reply(sock_dir):
    path = os.path.join(sock_dir, "nav.sock")

    async def on_conn(reader, writer):
        await reader.readline()
        writer.write(b"not json\n")
        await writer.drain()
        writer.close()

    async def go():
        server = await asyncio.start_unix_server(on_conn, path=path)
        try:
            return await kiosk_nav.request({"op": "ping"}, path)
        finally:
            server.close()
            await server.wait_closed()

    assert asyncio.run(go()) is None


# -- which origin the debugging fallback keeps -------------------------------


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True),
    ("localhost", True),
    ("LOCALHOST", True),
    ("::1", True),
    ("[::1]", True),
    ("kitchen-pi", True),
    ("kitchen-pi.local", False),
    ("pantry.example.com", False),
    ("192.168.1.50", False),
    ("", False),
])
def test_is_local_host(host, expected):
    assert kiosk_nav.is_local_host(host, hostname="Kitchen-Pi") is expected


def test_dotted_hostname_never_counts():
    assert kiosk_nav.is_local_host("kitchen-pi.lan", hostname="kitchen-pi.lan") is False


@pytest.mark.parametrize("page_url,base_url,expected", [
    # A loopback page keeps its own origin, whatever base_url says.
    ("http://localhost:9284/ui/?kiosk=1", "http://127.0.0.1:9284", "http://localhost:9284"),
    ("http://localhost/ui/?kiosk=1", "http://localhost:80", "http://localhost"),
    # A page that wandered off goes back to localhost on the app's port.
    ("https://pantry.example.com/ui/", "http://127.0.0.1:9284", "http://localhost:9284"),
    ("chrome-error://chromewebdata/", "http://localhost:80", "http://localhost"),
    ("about:blank", "https://127.0.0.1:8443", "https://localhost:8443"),
    # A deck whose app is on another machine keeps base_url's origin, as it
    # always has: nothing answers on this device's localhost.
    ("http://192.168.1.50:9284/ui/", "http://192.168.1.50:9284", "http://192.168.1.50:9284"),
    ("https://pantry.example.com/ui/", "http://192.168.1.50:9284", "http://192.168.1.50:9284"),
    ("about:blank", "http://user:pw@192.168.1.50:9284", "http://192.168.1.50:9284"),
    # This device's bare name counts as local, for the page and for base_url.
    ("http://kitchen-pi:9284/ui/", "http://192.168.1.50:9284", "http://kitchen-pi:9284"),
    ("about:blank", "http://kitchen-pi:9284", "http://localhost:9284"),
])
def test_kiosk_origin(page_url, base_url, expected):
    assert kiosk_nav.kiosk_origin(page_url, base_url, hostname="kitchen-pi") == expected
