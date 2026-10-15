"""The request gate and the response headers it stamps, pinned route by route.

The middleware in main.py decides who gets in and stamps the security headers,
the session cookie, and the compression on the way out. This pins the status,
every response header (in order), and the cookie attributes for a spread of
callers: signed out, signed in as admin and viewer, the kitchen screen, a
satellite forwarding to its server, a demo instance, a locked kiosk, a fresh
install, static files, and an image path that must never be compressed.

The fixture was captured from the stacked @app.middleware layers of 0.21 and
then checked against the plain ASGI gate that replaced them: status, cookies,
redirects and every security header came out the same. The one difference is
compression framing. The old layers re-chunked every body, which made GZip
compress even a 40 byte reply and drop its Content-Length; GZip now sees the
body whole, so a reply under its 500 byte minimum goes out uncompressed with a
Content-Length (and without Vary: Accept-Encoding), and a compressed one keeps
its Content-Length.

The expected values live in tests/fixtures/middleware_headers.json. Rebuild
them with PIN_MIDDLEWARE_HEADERS=1 only for an intended change, and say in the
commit what changed and why.
"""
from __future__ import annotations

import json
import os
import re
from base64 import b64encode
from pathlib import Path

import anyio
import httpx
import itsdangerous
import pytest

from app.config import settings
from app.passwords import hash_secret

SERVICE = Path(__file__).resolve().parents[1] / "service"
FIXTURE = Path(__file__).parent / "fixtures" / "middleware_headers.json"

_VOLATILE = {"etag", "last-modified", "content-length"}
_DROPPED = {"date", "server"}


def _normalize(r: httpx.Response) -> dict:
    headers = []
    for name, value in r.headers.multi_items():
        name = name.lower()
        if name in _DROPPED:
            continue
        if name in _VOLATILE:
            value = "*"
        elif name == "set-cookie":
            value = re.sub(r"^session=(?!null;)[^;]*", "session=*", value)
        headers.append([name, value])
    return {"status": r.status_code, "headers": headers}


def _session_cookie(data: dict) -> str:
    from app.main import app
    from starlette.middleware.sessions import SessionMiddleware
    secret = next(m.kwargs["secret_key"] for m in app.user_middleware
                  if m.cls is SessionMiddleware)
    signer = itsdangerous.TimestampSigner(str(secret))
    return signer.sign(b64encode(json.dumps(data).encode())).decode()


class FakeUpstream:
    async def request(self, method, url, headers=None, params=None, content=None):
        return httpx.Response(200, json={"forwarded": True, "path": url},
                              headers={"content-type": "application/json"})


def _base(monkeypatch, tmp_path):
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "remote_server_url", "", raising=False)
    monkeypatch.setattr(settings, "upstream_api_key", "", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret("hunter2"), raising=False)
    monkeypatch.setattr(settings, "api_key", "older-key", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", [], raising=False)
    monkeypatch.setattr(settings, "legacy_mirror", False, raising=False)
    monkeypatch.setattr(settings, "demo_mode", False, raising=False)
    monkeypatch.setattr(settings, "kiosk_pin", "", raising=False)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True, raising=False)


def _password_server(mp):
    pass


def _unconfigured(mp):
    mp.setattr(settings, "auth_required", False, raising=False)
    mp.setattr(settings, "auth_password", "", raising=False)
    mp.setattr(type(settings), "is_configured", lambda self: False, raising=False)


def _demo(mp):
    mp.setattr(settings, "auth_required", False, raising=False)
    mp.setattr(settings, "auth_password", "", raising=False)
    mp.setattr(settings, "demo_mode", True, raising=False)


def _satellite(mp, pin=""):
    from app import main
    mp.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    mp.setattr(settings, "remote_server_url", "http://server.test", raising=False)
    mp.setattr(settings, "upstream_api_key", "up-key", raising=False)
    mp.setattr(settings, "recipes_backend", "native", raising=False)
    mp.setattr(settings, "auth_required", False, raising=False)
    mp.setattr(settings, "auth_password", "", raising=False)
    mp.setattr(settings, "fleet_relay", {"v": 1, "server_has_password": False}, raising=False)
    mp.setattr(settings, "kiosk_pin", pin, raising=False)
    mp.setattr(settings, "kiosk_readonly_when_locked", False, raising=False)
    mp.setattr(main, "_satellite_fwd_client", FakeUpstream())


def _locked_satellite(mp):
    _satellite(mp, pin="4321")


def _read_only_satellite(mp):
    _satellite(mp, pin="4321")
    mp.setattr(settings, "kiosk_readonly_when_locked", True, raising=False)


LAN = {"peer": "192.168.1.50", "base_url": "http://testserver"}
LOOP = {"peer": "127.0.0.1", "base_url": "http://localhost"}
LOOP_TLS = {"peer": "127.0.0.1", "base_url": "https://localhost"}
HTML = {"Accept": "text/html"}

# name: (setup, client, method, path, headers, session)
CASES = {
    "anon_health": (_password_server, LAN, "GET", "/health", {}, None),
    "anon_page_redirects_to_login": (_password_server, LAN, "GET", "/ui/", HTML, None),
    "anon_json_refused": (_password_server, LAN, "GET", "/timers", {}, None),
    "anon_write_refused": (_password_server, LAN, "POST", "/timers", {}, None),
    "anon_login_page": (_password_server, LAN, "GET", "/ui/login", HTML, None),
    "anon_head_health": (_password_server, LAN, "HEAD", "/health", {}, None),
    "anon_root": (_password_server, LAN, "GET", "/", HTML, None),
    "anon_not_found": (_password_server, LAN, "GET", "/no/such/route", {}, None),
    "cross_site_write": (_password_server, LAN, "POST", "/timers",
                         {"Sec-Fetch-Site": "cross-site"}, None),
    "same_site_write": (_password_server, LAN, "POST", "/timers",
                        {"Sec-Fetch-Site": "same-site"}, {"authed": True, "role": "admin"}),
    "admin_json": (_password_server, LAN, "GET", "/timers", {}, {"authed": True, "role": "admin"}),
    "admin_page": (_password_server, LAN, "GET", "/ui/convert", HTML,
                   {"authed": True, "role": "admin"}),
    "admin_tls_json": (_password_server, LOOP_TLS, "GET", "/timers", {},
                       {"authed": True, "role": "admin"}),
    "viewer_settings_write": (_password_server, LAN, "POST", "/setup/save", {},
                              {"authed": True, "role": "viewer"}),
    "key_json": (_password_server, LAN, "GET", "/timers", {"X-API-Key": "older-key"}, None),
    "bad_key_json": (_password_server, LAN, "GET", "/timers", {"X-API-Key": "nope"}, None),
    "kiosk_json": (_password_server, LOOP, "GET", "/timers", {}, "kiosk"),
    "kiosk_tls_json": (_password_server, LOOP_TLS, "GET", "/timers", {}, "kiosk"),
    "static_css": (_password_server, LAN, "GET", "/static/css/base.css", {}, None),
    "static_head": (_password_server, LAN, "HEAD", "/static/css/base.css", {}, None),
    "static_missing": (_password_server, LAN, "GET", "/static/css/none.css", {}, None),
    "camera_path_never_gzipped": (_password_server, LAN, "GET", "/ui/camera/0/snapshot", {},
                                  {"authed": True, "role": "admin"}),
    "unconfigured_page": (_unconfigured, LAN, "GET", "/ui/", HTML, None),
    "unconfigured_kiosk_page": (_unconfigured, LAN, "GET", "/ui/?kiosk=1", HTML, None),
    "unconfigured_setup_bypass": (_unconfigured, LAN, "GET", "/health", {}, None),
    "demo_write_json": (_demo, LAN, "POST", "/timers", {}, None),
    "demo_write_html": (_demo, LAN, "POST", "/timers",
                        {"Accept": "text/html", "Referer": "http://testserver/ui/timers"}, None),
    "satellite_forwarded": (_satellite, LOOP, "GET", "/gadgets/devices", {}, None),
    "satellite_forwarded_bad_path": (_satellite, LOOP, "GET", "/gadgets/%2e%2e/ha", {}, None),
    "satellite_local": (_satellite, LOOP, "GET", "/health", {}, None),
    "pin_locked_page": (_locked_satellite, LOOP, "GET", "/ui/", HTML, None),
    "pin_read_only_page": (_read_only_satellite, LOOP, "GET", "/ui/pin", HTML, None),
    "pin_read_only_write": (_read_only_satellite, LOOP, "POST", "/ui/timers", {}, None),
    "satellite_forwarded_write": (_satellite, LOOP, "POST", "/recipes/import", {}, None),
    "satellite_forwarded_remote": (_satellite, LAN, "GET", "/mealie/recipes", {}, None),
    "pin_screen_open": (_locked_satellite, LOOP, "GET", "/ui/pin", HTML, None),
}


def _run(name, encoding, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.services import auth_state
    setup, where, method, path, headers, session = CASES[name]
    _base(monkeypatch, tmp_path)
    setup(monkeypatch)
    client = TestClient(app, client=(where["peer"], 50000), base_url=where["base_url"],
                        follow_redirects=False)
    headers = dict(headers)
    headers["Accept-Encoding"] = encoding
    if session == "kiosk":
        session = {"authed": True, "role": "kiosk", "kiosk_gen": auth_state.kiosk_gen(),
                   "kiosk_day": "2000-01-01"}
    if session:
        client.cookies.set("session", _session_cookie(session))
    kw = {"json": {}} if method == "POST" else {}
    return _normalize(client.request(method, path, headers=headers, **kw))


ENCODINGS = ("identity", "gzip")
PARAMS = [(n, e) for n in CASES for e in ENCODINGS]


def _expected():
    return json.loads(FIXTURE.read_text()) if FIXTURE.exists() else {}


@pytest.mark.parametrize("name,encoding", PARAMS)
def test_response_headers_are_pinned(name, encoding, monkeypatch, tmp_path):
    got = _run(name, encoding, monkeypatch, tmp_path)
    key = f"{name}/{encoding}"
    if os.environ.get("PIN_MIDDLEWARE_HEADERS"):
        data = _expected()
        data[key] = got
        FIXTURE.write_text(json.dumps(dict(sorted(data.items())), indent=1) + "\n")
        return
    assert got == _expected()[key]


def test_every_pinned_case_still_runs():
    assert sorted(_expected()) == sorted(f"{n}/{e}" for n, e in PARAMS)


# -- streaming -------------------------------------------------------------

def test_a_streamed_response_is_not_buffered(monkeypatch, tmp_path):
    """A camera stream or a download reaches the client chunk by chunk: the
    route's second chunk waits until the first one has left the app."""
    from fastapi.responses import StreamingResponse
    from app.main import app

    _base(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)

    first_out = anyio.Event()

    async def frames():
        yield b"--frame\r\n" + b"x" * 1000
        with anyio.fail_after(5):
            await first_out.wait()
        yield b"--frame\r\n" + b"y" * 1000

    async def stream_route():
        return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame")

    app.router.add_api_route("/ui/camera/streamtest", stream_route, methods=["GET"])
    route = app.router.routes.pop()
    app.router.routes.insert(0, route)
    sent = []

    async def drive():
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": "/ui/camera/streamtest",
            "raw_path": b"/ui/camera/streamtest", "root_path": "", "query_string": b"",
            "headers": [(b"host", b"localhost"), (b"accept-encoding", b"gzip")],
            "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 9284),
        }

        async def receive():
            await anyio.sleep_forever()

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                first_out.set()

        with anyio.fail_after(10):
            await app(scope, receive, send)

    try:
        anyio.run(drive)
    finally:
        app.router.routes.remove(route)
    start = sent[0]
    assert start["type"] == "http.response.start" and start["status"] == 200
    names = {k.lower() for k, _ in start["headers"]}
    assert b"content-encoding" not in names
    assert b"x-frame-options" in names and b"content-security-policy" in names
    bodies = [m["body"] for m in sent if m["type"] == "http.response.body" and m.get("body")]
    assert len(bodies) >= 2
