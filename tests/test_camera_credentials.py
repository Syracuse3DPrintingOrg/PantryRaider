"""Camera logins never reach a browser or a satellite
(service/app/services/cameras.py and the /ui/camera routes).

A manual camera whose address carries a login (user:pass@, ?password=, a token)
is fetched or relayed by the server; the page only ever sees the app's own
proxy path. Clean manual addresses behave as before (a clean stream is still a
redirect), and Home Assistant and Reolink cameras are unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import cameras, egress  # noqa: E402

_USERINFO = "http://admin:hunter2@192.0.2.40/snap.jpg"
_QUERY = "http://192.0.2.41/cgi-bin/snap.cgi?user=admin&password=hunter2"
_STREAM = "http://192.0.2.42/video.mjpg?token=abc123secret"
_CLEAN = "http://192.0.2.43/snap.jpg"


@pytest.fixture(autouse=True)
def _no_ha(monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "http://ha.example:8123", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "tok", raising=False)


# -- is_credentialed_url ------------------------------------------------------

@pytest.mark.parametrize("url", [
    _USERINFO, _QUERY, _STREAM, "http://user@192.0.2.1/x",
    "http://h/x?PWD=1", "http://h/x?Pass=1", "http://h/x?access_token=1",
    "http://h/x?auth=1", "http://h/x?key=1", "http://h/x?apikey=1", "http://h/x?api_key=1",
    "http://h/x?username=a", "rtsp://admin:pw@192.0.2.5:554/h264Preview_01_main",
    "http://ha.example:8123/api/camera_proxy/camera.door?token=t",
    "http://ha.example:8123/api/camera_proxy_stream/camera.door",
])
def test_credentialed_urls(url):
    assert cameras.is_credentialed_url(url) is True


@pytest.mark.parametrize("url", [
    "", None, _CLEAN, "http://192.0.2.1/x?channel=1&rs=abc",
    "https://cam.example/live.m3u8", "http://h/x?keyframe=1",
])
def test_clean_urls(url):
    assert cameras.is_credentialed_url(url) is False


@pytest.mark.parametrize("url", [
    # A browser reads a login in all of these, though urlsplit finds no host.
    "http:admin:hunter2@192.0.2.1/snap.jpg", "http:/admin:hunter2@192.0.2.1/x",
    "http:\\\\admin:hunter2@192.0.2.1\\x", "HTTP://admin:hunter2@192.0.2.1/x",
    "http://ad\tmin:hun\nter2@192.0.2.1/x",
    # Vendor spellings of a password or token key.
    "http://h/x?usr=a&pw=b", "http://h/x?loginuse=a&loginpas=b", "http://h/x?passwd=b",
    "http://h/x?auth_token=b", "http://h/x?client_secret=b", "http://h/x?%70assword=b",
])
def test_credentialed_urls_in_browser_spellings(url):
    assert cameras.is_credentialed_url(url) is True


def test_uppercase_scheme_credentialed_snapshot_is_still_fetched_by_the_server():
    # The page is pointed at the proxy for it, so the proxy must fetch it
    # rather than answer "no snapshot".
    url, headers = cameras.proxied_snapshot({"snapshot_url": "HTTP://admin:hunter2@192.0.2.1/x"})
    assert url == "HTTP://admin:hunter2@192.0.2.1/x" and headers is None


def test_redact_hides_the_login_in_either_spelling():
    url = "http://admin:p%40ss@192.0.2.1/x?token=s3cret"
    text = cameras._redact(Exception("failed for p@ss and p%40ss and s3cret"), url)
    assert "p@ss" not in text and "p%40ss" not in text and "s3cret" not in text


# -- camera_sources -----------------------------------------------------------

def test_camera_sources_proxies_credentialed_manual_urls():
    out = cameras.camera_sources([
        {"name": "A", "snapshot_url": _USERINFO, "stream_url": _STREAM},
        {"name": "B", "snapshot_url": _QUERY},
        {"name": "C", "snapshot_url": _CLEAN, "stream_url": "https://cam.example/live.m3u8"},
        {"name": "D", "stream_url": "rtsp://admin:pw@192.0.2.5:554/main"},
    ])
    assert out[0]["snapshot_src"] == "ui/camera/0/snapshot"
    assert out[0]["stream_src"] == "ui/camera/0/stream"
    assert out[1]["snapshot_src"] == "ui/camera/1/snapshot" and out[1]["stream_src"] == ""
    # Clean addresses are unchanged, so an HLS stream still plays directly.
    assert out[2]["snapshot_src"] == _CLEAN
    assert out[2]["stream_src"] == "https://cam.example/live.m3u8"
    assert out[3]["stream_src"] == ""
    assert "hunter2" not in repr(out) and "abc123secret" not in repr(out) and "pw@" not in repr(out)


# -- sanitize_camera_for_satellite --------------------------------------------

def test_sanitize_reolink_drops_login_and_relays():
    out = cameras.sanitize_camera_for_satellite({
        "name": "Door", "source": "reolink", "host": "192.0.2.50", "port": "",
        "channel": 1, "username": "admin", "password": "hunter2",
        "stream_quality": "sub", "device_type": "doorbell", "two_way_talk": True,
        "popup_types": ["person"],
    })
    assert out == {"name": "Door", "source": "reolink", "channel": 1,
                   "stream_quality": "sub", "device_type": "doorbell",
                   "two_way_talk": True, "popup_types": ["person"],
                   "relay": True, "has_stream": False}


def test_sanitize_manual_blanks_credentialed_urls_only():
    out = cameras.sanitize_camera_for_satellite(
        {"name": "Shed", "snapshot_url": _QUERY, "stream_url": _CLEAN,
         "api_token": "x", "secret_sauce": "y"})
    assert "snapshot_url" not in out and out["stream_url"] == _CLEAN
    assert out["relay"] is True and out["has_stream"] is True
    assert "api_token" not in out and "secret_sauce" not in out


def test_sanitize_clean_manual_camera_is_not_relayed():
    out = cameras.sanitize_camera_for_satellite({"name": "Yard", "snapshot_url": _CLEAN})
    assert out == {"name": "Yard", "snapshot_url": _CLEAN, "relay": False, "has_stream": False}


def test_sanitize_ha_camera_keeps_entity_and_relays():
    out = cameras.sanitize_camera_for_satellite(
        {"name": "Porch", "snapshot_url": "http://ha.example:8123/api/camera_proxy/camera.porch?token=t"})
    assert out["ha_entity"] == "camera.porch"
    assert "snapshot_url" not in out
    assert out["relay"] is True and out["has_stream"] is True


def test_sanitize_non_dict():
    assert cameras.sanitize_camera_for_satellite("x") == {}


# -- snapshot_response / stream_response --------------------------------------

class _Resp:
    def __init__(self, status=200, content=b"\xff\xd8jpeg", ctype="image/jpeg"):
        self.status_code = status
        self.content = content
        self.headers = {"content-type": ctype}


class _Upstream:
    status_code = 200
    headers = {"content-type": "multipart/x-mixed-replace; boundary=frame"}

    def __init__(self):
        self.closed = False

    async def aiter_raw(self):
        for chunk in (b"--frame\r\n", b"jpegdata"):
            yield chunk

    async def aclose(self):
        self.closed = True


class _GuardedClient:
    """Records how the guarded client was asked to fetch, and answers."""
    calls = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, **k):
        _GuardedClient.calls.append(("get", url, headers, self.kwargs))
        return _Resp()

    def build_request(self, method, url, headers=None):
        _GuardedClient.calls.append(("stream", url, headers, self.kwargs))
        return (method, url)

    async def send(self, req, stream=False):
        assert stream is True  # relayed as it arrives, never buffered whole
        return _Upstream()

    async def aclose(self):
        pass


@pytest.fixture
def guarded(monkeypatch):
    _GuardedClient.calls = []
    seen = {}

    def fake(*, allow_private=False, **kw):
        seen["allow_private"] = allow_private
        return _GuardedClient(**kw)

    monkeypatch.setattr(egress, "guarded_async_client", fake)
    return seen


@pytest.mark.anyio
async def test_credentialed_http_snapshot_is_fetched_by_the_server(guarded):
    resp = await cameras.snapshot_response({"name": "A", "snapshot_url": _USERINFO})
    assert resp.status_code == 200 and resp.body == b"\xff\xd8jpeg"
    assert _GuardedClient.calls[0][1] == _USERINFO
    assert _GuardedClient.calls[0][3]["timeout"] == 8.0
    assert guarded["allow_private"] is True


@pytest.mark.anyio
async def test_credentialed_non_http_snapshot_is_never_redirected(guarded):
    resp = await cameras.snapshot_response(
        {"name": "A", "snapshot_url": "rtsp://admin:hunter2@192.0.2.5/x"})
    assert resp.status_code == 404
    assert "location" not in resp.headers
    assert b"hunter2" not in resp.body


@pytest.mark.anyio
async def test_clean_non_http_snapshot_still_redirects(guarded):
    resp = await cameras.snapshot_response({"name": "A", "snapshot_url": "rtsp://192.0.2.5/x"})
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "rtsp://192.0.2.5/x"


@pytest.mark.anyio
async def test_credentialed_stream_is_relayed_not_redirected(guarded):
    resp = await cameras.stream_response({"name": "A", "stream_url": _STREAM})
    assert resp.status_code == 200
    assert resp.media_type.startswith("multipart/x-mixed-replace")
    assert _GuardedClient.calls[0][0] == "stream" and _GuardedClient.calls[0][1] == _STREAM
    assert _GuardedClient.calls[0][3]["timeout"] is None
    assert "location" not in resp.headers


@pytest.mark.anyio
async def test_clean_stream_still_redirects(guarded):
    resp = await cameras.stream_response({"name": "A", "stream_url": "http://192.0.2.43/v.mjpg"})
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "http://192.0.2.43/v.mjpg"
    assert _GuardedClient.calls == []


@pytest.mark.anyio
async def test_ha_stream_still_relayed_with_bearer(guarded):
    resp = await cameras.stream_response({"name": "Porch", "ha_entity": "camera.porch"})
    assert resp.status_code == 200
    _kind, url, headers, _kw = _GuardedClient.calls[0]
    assert url == "http://ha.example:8123/api/camera_proxy_stream/camera.porch"
    assert headers == {"Authorization": "Bearer tok"}


@pytest.mark.anyio
async def test_credentialed_stream_to_loopback_is_refused(monkeypatch):
    # The real guarded client: the SSRF guard still applies to the new relay.
    import socket
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                                ("127.0.0.1", 0))])
    resp = await cameras.stream_response(
        {"name": "A", "stream_url": "http://admin:pw@127.0.0.1:9284/health"})
    assert resp.status_code == 400
    assert b"pw" not in resp.body


def test_unreachable_error_text_drops_the_login():
    err = RuntimeError(f"connect failed for {_QUERY}")
    assert "hunter2" not in cameras._redact(err, _QUERY)
    err = RuntimeError("401 for password=hunter2")
    assert "hunter2" not in cameras._redact(err, _QUERY)


@pytest.mark.anyio
async def test_routes_delegate_and_keep_unknown_index(monkeypatch, guarded):
    from app.routers import ui as ui_router
    monkeypatch.setattr(settings, "streamdeck_cameras",
                        [{"name": "A", "snapshot_url": _USERINFO, "stream_url": _STREAM}],
                        raising=False)
    assert (await ui_router.camera_snapshot(0)).status_code == 200
    assert (await ui_router.camera_stream(0)).status_code == 200
    assert (await ui_router.camera_snapshot(5)).status_code == 404
    assert (await ui_router.camera_stream(-1)).status_code == 404


@pytest.mark.parametrize("ctype,expected", [
    ("image/jpeg", "image/jpeg"), ("multipart/x-mixed-replace; boundary=f", "multipart/x-mixed-replace; boundary=f"),
    ("application/vnd.apple.mpegurl", "application/vnd.apple.mpegurl"),
    ("text/html", "image/jpeg"), ("TEXT/HTML; charset=utf-8", "image/jpeg"),
    ("image/svg+xml", "image/jpeg"), ("application/javascript", "image/jpeg"),
    ("", "image/jpeg"), (None, "image/jpeg"),
])
def test_feed_media_type_never_serves_a_page_from_the_app(ctype, expected):
    # A relayed feed is served from the app's own address, so a camera that
    # answers with a web page or script must not have it run there.
    assert cameras._feed_media_type(ctype, "image/jpeg") == expected


@pytest.mark.anyio
async def test_server_fetched_snapshot_answering_html_is_not_served_as_html(monkeypatch, guarded):
    monkeypatch.setattr(_GuardedClient, "get", _html_get, raising=True)
    resp = await cameras.snapshot_response({"name": "A", "snapshot_url": _USERINFO})
    assert resp.status_code == 200
    assert resp.media_type == "image/jpeg"


async def _html_get(self, url, headers=None, **k):
    return _Resp(content=b"<script>alert(1)</script>", ctype="text/html")
