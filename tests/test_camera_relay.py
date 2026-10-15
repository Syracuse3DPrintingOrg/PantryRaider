"""Satellites show the main server's cameras without holding their logins
(service/app/services/cameras.py and the /ui/camera routes).

A satellite whose main server relays cameras gets each camera with its login
left out (``relay`` True). Its kiosk page points at the app's own proxy path,
and the app asks the main server for the feed by name, so a camera list whose
order drifted never shows a different camera. Every failure costs only the
picture: a small JSON 404 or 502, never an exception.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import cameras, egress  # noqa: E402

_REMOTE = "http://192.0.2.10:9284"
_KEY = "upstream-key-not-a-secret"
_LOGIN_URL = "http://admin:hunter2@192.168.1.40/snap.jpg"
_TOKEN_URL = "http://192.168.1.41/video.mjpg?token=abc123secret"
_CLEAN = "http://192.168.1.43/snap.jpg"
_HLS = "https://cam.example/live.m3u8"
_LOOPBACK = ("127.0.0.1", 50000)


def _sanitized_list() -> list:
    """What a relay satellite holds after the server sanitized its cameras."""
    return [
        cameras.sanitize_camera_for_satellite({"name": "Porch", "ha_entity": "camera.porch"}),
        cameras.sanitize_camera_for_satellite(
            {"name": "Door", "snapshot_url": _LOGIN_URL, "stream_url": _TOKEN_URL}),
        cameras.sanitize_camera_for_satellite({
            "name": "Bell", "source": "reolink", "host": "192.168.1.50",
            "username": "admin", "password": "hunter2", "channel": 0}),
        cameras.sanitize_camera_for_satellite({"name": "Yard", "snapshot_url": _CLEAN,
                                               "stream_url": _HLS}),
    ]


# -- camera_sources on a sanitized list ---------------------------------------

def test_camera_sources_on_a_sanitized_list_never_carries_a_login(monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "", raising=False)
    cams = _sanitized_list()
    out = cameras.camera_sources(cams)
    porch, door, bell, yard = out
    assert porch["snapshot_src"] == "ui/camera/0/snapshot"
    assert porch["stream_src"] == "ui/camera/0/stream" and porch["is_ha"] is True
    assert door["snapshot_src"] == "ui/camera/1/snapshot"
    assert door["stream_src"] == "ui/camera/1/stream" and door["is_ha"] is False
    assert bell["snapshot_src"] == "ui/camera/2/snapshot" and bell["stream_src"] == ""
    # A clean address stays direct, so HLS still plays in the browser.
    assert yard["snapshot_src"] == _CLEAN and yard["stream_src"] == _HLS
    text = json.dumps(cams) + json.dumps(out)
    for secret in ("hunter2", "abc123secret", "admin:", "?token=", "password"):
        assert secret not in text


def test_relay_manual_entry_without_a_stream_has_no_stream_path():
    out = cameras.camera_sources([{"name": "Shed", "relay": True, "has_stream": False}])
    assert out == [{"name": "Shed", "snapshot_src": "ui/camera/0/snapshot",
                    "stream_src": "", "is_ha": False}]
    out = cameras.camera_sources([{"name": "Shed", "relay": True, "has_stream": True}])
    assert out[0]["stream_src"] == "ui/camera/0/stream"


def test_relay_entry_keeps_a_clean_stream_it_was_sent():
    entry = cameras.sanitize_camera_for_satellite(
        {"name": "Gate", "snapshot_url": _LOGIN_URL, "stream_url": _HLS})
    assert entry["relay"] is True and entry["stream_url"] == _HLS
    out = cameras.camera_sources([entry])
    assert out[0]["snapshot_src"] == "ui/camera/0/snapshot"
    assert out[0]["stream_src"] == _HLS


def test_a_login_bearing_rtsp_stream_is_no_relayable_stream():
    entry = cameras.sanitize_camera_for_satellite(
        {"name": "Garage", "snapshot_url": _CLEAN,
         "stream_url": "rtsp://admin:pw@192.168.1.5:554/main"})
    assert entry["relay"] is True and entry["has_stream"] is False
    assert cameras.camera_sources([entry])[0]["stream_src"] == ""


# -- resolve_camera_by_name ---------------------------------------------------

_NAMED = [{"name": "Front"}, {"name": " Back "}, "not-a-camera", {"name": "Side"}]


@pytest.mark.parametrize("idx,name,expected", [
    (0, "", 0), (3, "", 3), (2, "", 2),        # no name: the index when in range
    (4, "", None), (-1, "", None),            # out of range
    (1, "back", 1), (1, "  BACK", 1),          # the index's own camera matches
    (0, "Back", 1), (3, "front", 0),           # moved: the first camera with the name
    (9, "Side", 3),                            # out-of-range index, known name
    (0, "Nope", None), (1, "Front Door", None),  # unknown name, never another camera
])
def test_resolve_camera_by_name(idx, name, expected):
    assert cameras.resolve_camera_by_name(_NAMED, idx, name) == expected


def test_resolve_camera_by_name_on_an_empty_list():
    assert cameras.resolve_camera_by_name([], 0, "") is None
    assert cameras.resolve_camera_by_name(None, 0, "Back") is None


# -- the relay on a satellite -------------------------------------------------

class _Recorder:
    """A mock transport for the main server that records what it was asked."""

    def __init__(self, handler):
        self.requests: list = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


@pytest.fixture
def relay_satellite(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "remote_server_url", _REMOTE + "/", raising=False)
    monkeypatch.setattr(settings, "upstream_api_key", _KEY, raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "fleet_relay", {"v": 1, "caps": ["camera"]}, raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "", raising=False)
    monkeypatch.setattr(settings, "streamdeck_cameras", [
        {"name": "Yard", "snapshot_url": _CLEAN},
        cameras.sanitize_camera_for_satellite(
            {"name": "Door", "snapshot_url": _LOGIN_URL, "stream_url": _TOKEN_URL}),
    ], raising=False)
    try:
        yield monkeypatch
    finally:
        os.chdir(cwd)


def _use_transport(monkeypatch, handler) -> _Recorder:
    recorder = _Recorder(handler)

    def client(timeout):
        return httpx.AsyncClient(transport=httpx.MockTransport(recorder),
                                 timeout=timeout, follow_redirects=False)

    monkeypatch.setattr(cameras, "_upstream_client", client)
    return recorder


def _kiosk():
    from app.main import app
    return TestClient(app, client=_LOOPBACK, base_url="http://localhost")


def test_should_relay_needs_a_relay_satellite_and_a_relay_entry(relay_satellite):
    assert cameras.should_relay({"name": "Door", "relay": True}) is True
    assert cameras.should_relay({"name": "Yard", "relay": False}) is False
    assert cameras.should_relay({"name": "Yard"}) is False
    assert cameras.should_relay("Door") is False
    relay_satellite.setattr(settings, "fleet_relay", {"v": 1, "caps": ["ai"]}, raising=False)
    assert cameras.should_relay({"name": "Door", "relay": True}) is False
    relay_satellite.setattr(settings, "fleet_relay", {"v": 1, "caps": ["camera"]}, raising=False)
    relay_satellite.setattr(settings, "deployment_mode", "server", raising=False)
    assert cameras.should_relay({"name": "Door", "relay": True}) is False


def test_snapshot_is_relayed_to_the_server_by_name(relay_satellite):
    rec = _use_transport(relay_satellite, lambda req: httpx.Response(
        200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"}))
    r = _kiosk().get("/ui/camera/1/snapshot")
    assert r.status_code == 200 and r.content == b"\xff\xd8jpeg"
    assert r.headers["content-type"] == "image/jpeg"
    (req,) = rec.requests
    assert req.method == "GET"
    assert str(req.url) == _REMOTE + "/ui/camera/1/snapshot?name=Door"
    assert req.headers["X-API-Key"] == _KEY
    assert req.headers["X-PR-Caps"] == "relay1"
    assert req.headers["X-PR-Client"].startswith("satellite/")


def test_relayed_name_is_quoted(relay_satellite):
    relay_satellite.setattr(settings, "streamdeck_cameras",
                            [{"name": "Back door & gate/1", "relay": True}], raising=False)
    rec = _use_transport(relay_satellite, lambda req: httpx.Response(
        200, content=b"x", headers={"content-type": "image/png"}))
    assert _kiosk().get("/ui/camera/0/snapshot").status_code == 200
    assert rec.requests[0].url.raw_path.endswith(b"?name=Back%20door%20%26%20gate%2F1")


def test_relayed_page_is_never_served_as_a_page(relay_satellite):
    _use_transport(relay_satellite, lambda req: httpx.Response(
        200, content=b"<script>alert(1)</script>", headers={"content-type": "text/html"}))
    r = _kiosk().get("/ui/camera/1/snapshot")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"


def test_server_404_is_unknown_camera(relay_satellite):
    _use_transport(relay_satellite, lambda req: httpx.Response(
        404, json={"detail": "Unknown camera."}))
    r = _kiosk().get("/ui/camera/1/snapshot")
    assert r.status_code == 404 and r.json() == {"detail": "Unknown camera."}


def test_other_server_errors_are_a_502(relay_satellite):
    _use_transport(relay_satellite, lambda req: httpx.Response(
        500, text="Traceback (secret detail)"))
    r = _kiosk().get("/ui/camera/1/snapshot")
    assert r.status_code == 502
    assert r.json() == {"detail": "The main server could not show this camera."}


def test_a_redirect_from_the_server_is_not_followed(relay_satellite):
    rec = _use_transport(relay_satellite, lambda req: httpx.Response(
        307, headers={"location": "http://192.168.1.99/elsewhere"}))
    r = _kiosk().get("/ui/camera/1/snapshot")
    assert r.status_code == 502 and len(rec.requests) == 1


def test_unreachable_server_is_a_502(relay_satellite):
    def boom(req):
        raise httpx.ConnectError("connection refused", request=req)
    _use_transport(relay_satellite, boom)
    r = _kiosk().get("/ui/camera/1/snapshot")
    assert r.status_code == 502
    assert r.json() == {"detail": "The main server is not reachable. This will return when it is."}
    r = _kiosk().get("/ui/camera/1/stream")
    assert r.status_code == 502


def test_non_relay_entry_on_the_same_satellite_is_not_forwarded(relay_satellite):
    rec = _use_transport(relay_satellite, lambda req: httpx.Response(200, content=b"x"))
    seen = []

    async def local(entry):
        from fastapi.responses import Response
        seen.append(entry)
        return Response(content=b"local", media_type="image/jpeg")

    relay_satellite.setattr(cameras, "snapshot_response", local)
    r = _kiosk().get("/ui/camera/0/snapshot")
    assert r.status_code == 200 and r.content == b"local"
    assert rec.requests == [] and seen[0]["name"] == "Yard"


def test_unknown_index_on_a_satellite_is_a_404(relay_satellite):
    rec = _use_transport(relay_satellite, lambda req: httpx.Response(200, content=b"x"))
    r = _kiosk().get("/ui/camera/7/snapshot")
    assert r.status_code == 404 and r.json() == {"detail": "Unknown camera."}
    assert rec.requests == []


@pytest.mark.parametrize("seg", ["+1", "-1", "1.0", "%201", "1_0", "1%20"])
def test_an_index_that_is_not_plain_digits_reaches_no_camera(relay_satellite, seg):
    # The satellite's access rules recognise a camera feed by a plain-digit
    # index. Any other spelling an int parser accepts must not reach a camera,
    # or an anonymous caller could use this device as a relay by name.
    rec = _use_transport(relay_satellite, lambda req: httpx.Response(
        200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"}))
    from app.main import app
    lan = TestClient(app, client=("192.168.1.20", 50000))
    for kind in ("snapshot", "stream"):
        r = lan.get(f"/ui/camera/{seg}/{kind}?name=Door", follow_redirects=False)
        assert r.status_code in (403, 404), (seg, kind, r.status_code)
    # Even the kiosk, which the access rules let through, gets no camera.
    r = _kiosk().get(f"/ui/camera/{seg}/snapshot?name=Door")
    assert r.status_code == 404 and r.json() == {"detail": "Unknown camera."}
    assert rec.requests == []


def test_anonymous_lan_caller_cannot_use_a_passwordless_satellite_as_a_relay(relay_satellite):
    rec = _use_transport(relay_satellite, lambda req: httpx.Response(
        200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"}))
    from app.main import app
    lan = TestClient(app, client=("192.168.1.20", 50000))
    assert lan.get("/ui/camera/1/snapshot?name=Door").status_code == 403
    assert rec.requests == []


class _Body(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.anyio
async def test_stream_relay_yields_the_chunks_and_closes_the_upstream(relay_satellite):
    body = _Body([b"--frame\r\n", b"jpegdata", b"--frame\r\n"])
    rec = _use_transport(relay_satellite, lambda req: httpx.Response(
        200, headers={"content-type": "multipart/x-mixed-replace; boundary=frame"},
        stream=body))
    entry = settings.streamdeck_cameras[1]
    resp = await cameras.relay_stream(1, entry)
    assert resp.status_code == 200
    assert resp.media_type == "multipart/x-mixed-replace; boundary=frame"
    got = [chunk async for chunk in resp.body_iterator]
    assert b"".join(got) == b"--frame\r\njpegdata--frame\r\n"
    await resp.background()
    assert body.closed is True
    (req,) = rec.requests
    assert str(req.url) == _REMOTE + "/ui/camera/1/stream?name=Door"
    assert req.headers["X-API-Key"] == _KEY


@pytest.mark.anyio
async def test_stream_relay_uses_a_read_timeout(relay_satellite):
    seen = {}

    def client(timeout):
        seen["timeout"] = timeout
        return httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(404, json={"detail": "Unknown camera."})),
            timeout=timeout)

    relay_satellite.setattr(cameras, "_upstream_client", client)
    resp = await cameras.relay_stream(1, settings.streamdeck_cameras[1])
    assert resp.status_code == 404
    t = seen["timeout"]
    assert (t.connect, t.read, t.write, t.pool) == (5.0, 30.0, 10.0, 5.0)


@pytest.mark.anyio
async def test_a_stalled_stream_ends_quietly(relay_satellite):
    class _Stall(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"first"
            raise httpx.ReadTimeout("stalled")

        async def aclose(self):
            _Stall.closed = True

    _use_transport(relay_satellite, lambda req: httpx.Response(
        200, headers={"content-type": "multipart/x-mixed-replace"}, stream=_Stall()))
    resp = await cameras.relay_stream(1, settings.streamdeck_cameras[1])
    got = [chunk async for chunk in resp.body_iterator]
    assert got == [b"first"]
    await resp.background()
    assert _Stall.closed is True


def test_stream_route_relays_on_a_satellite(relay_satellite):
    rec = _use_transport(relay_satellite, lambda req: httpx.Response(
        200, headers={"content-type": "multipart/x-mixed-replace; boundary=f"},
        stream=_Body([b"--f\r\n", b"frame"])))
    r = _kiosk().get("/ui/camera/1/stream")
    assert r.status_code == 200 and r.content == b"--f\r\nframe"
    assert str(rec.requests[0].url) == _REMOTE + "/ui/camera/1/stream?name=Door"


# -- the server resolves by name ----------------------------------------------

class _Guarded:
    calls: list = []

    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, **k):
        _Guarded.calls.append(url)
        return httpx.Response(200, content=b"\xff\xd8" + url.encode(),
                              headers={"content-type": "image/jpeg"})


@pytest.fixture
def server(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "gk", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "streamdeck_cameras", [
        {"name": "Front", "snapshot_url": "http://admin:pw@192.168.1.60/front.jpg"},
        {"name": "Back", "snapshot_url": "http://admin:pw@192.168.1.61/back.jpg"},
    ], raising=False)
    _Guarded.calls = []
    monkeypatch.setattr(egress, "guarded_async_client",
                        lambda *, allow_private=False, **kw: _Guarded(**kw))
    try:
        yield monkeypatch
    finally:
        os.chdir(cwd)


def test_server_resolves_the_camera_by_name(server):
    from app.main import app
    client = TestClient(app, client=("192.168.1.20", 50000))
    r = client.get("/ui/camera/0/snapshot", params={"name": "Back"})
    assert r.status_code == 200
    assert _Guarded.calls == ["http://admin:pw@192.168.1.61/back.jpg"]
    assert client.get("/ui/camera/0/snapshot", params={"name": "Nope"}).status_code == 404
    assert client.get("/ui/camera/0/stream", params={"name": "Nope"}).json() == {
        "detail": "Unknown camera."}
    # Old kiosks and old satellites send no name: the index alone, as before.
    _Guarded.calls = []
    assert client.get("/ui/camera/0/snapshot").status_code == 200
    assert _Guarded.calls == ["http://admin:pw@192.168.1.60/front.jpg"]


def test_server_fetches_a_login_url_and_never_redirects(server):
    from app.main import app
    client = TestClient(app, client=("192.168.1.20", 50000))
    r = client.get("/ui/camera/1/snapshot", follow_redirects=False)
    assert r.status_code == 200
    assert "location" not in r.headers
    assert _Guarded.calls == ["http://admin:pw@192.168.1.61/back.jpg"]


def test_a_relay_entry_on_a_server_is_never_relayed(server):
    # Relay flags only count on a satellite with the camera relay on.
    server.setattr(settings, "fleet_relay", {"v": 1, "caps": ["camera"]}, raising=False)
    assert cameras.should_relay({"name": "Back", "relay": True}) is False


# -- Reolink AI state ---------------------------------------------------------

@pytest.mark.anyio
async def test_reolink_ai_state_is_relayed_on_a_satellite(relay_satellite):
    state = {"code": 0, "value": {"people": {"alarm_state": 1}}}
    rec = _use_transport(relay_satellite, lambda req: httpx.Response(
        200, json={"ok": True, "state": state}))
    got = await cameras.fetch_reolink_ai_state(
        {"name": "Bell", "source": "reolink", "relay": True})
    assert got == state
    (req,) = rec.requests
    assert req.method == "POST"
    assert str(req.url) == _REMOTE + "/api/fleet/camera-ai"
    assert json.loads(req.content) == {"name": "Bell"}
    assert req.headers["X-API-Key"] == _KEY


@pytest.mark.anyio
@pytest.mark.parametrize("handler", [
    lambda req: httpx.Response(404, json={"detail": "Unknown camera."}),
    lambda req: httpx.Response(200, json={"ok": True, "state": "nope"}),
    lambda req: httpx.Response(200, json=["not", "a", "dict"]),
    lambda req: httpx.Response(200, text="not json"),
    lambda req: httpx.Response(502, json={"detail": "x"}),
])
async def test_reolink_ai_state_relay_failures_are_empty(relay_satellite, handler):
    _use_transport(relay_satellite, handler)
    assert await cameras.fetch_reolink_ai_state(
        {"name": "Bell", "source": "reolink", "relay": True}) == {}


@pytest.mark.anyio
async def test_reolink_ai_state_relay_unreachable_is_empty(relay_satellite):
    def boom(req):
        raise httpx.ConnectError("refused", request=req)
    _use_transport(relay_satellite, boom)
    assert await cameras.fetch_reolink_ai_state(
        {"name": "Bell", "source": "reolink", "relay": True}) == {}


@pytest.mark.anyio
async def test_reolink_ai_state_stays_local_for_a_full_entry(relay_satellite):
    rec = _use_transport(relay_satellite, lambda req: httpx.Response(200, json={}))
    seen = []

    async def local(entry, cmd, with_channel, timeout):
        seen.append(cmd)
        return {"local": True}

    relay_satellite.setattr(cameras, "_fetch_reolink_json", local)
    got = await cameras.fetch_reolink_ai_state(
        {"name": "Bell", "source": "reolink", "host": "192.168.1.50",
         "username": "admin", "password": "pw"})
    assert got == {"local": True} and seen == ["GetAiState"] and rec.requests == []


# -- diagnostics --------------------------------------------------------------

@pytest.mark.anyio
async def test_camera_diag_reports_relayed_cameras_without_tokens(relay_satellite):
    from app.routers import ui as ui_router
    relay_satellite.setattr(settings, "fleet_relay",
                            {"v": 1, "caps": ["camera", "ha"], "ha_available": True},
                            raising=False)
    relay_satellite.setattr(settings, "streamdeck_cameras", [
        cameras.sanitize_camera_for_satellite({"name": "Porch", "ha_entity": "camera.porch"}),
    ], raising=False)
    # An old copy of the token left on the device never shows up either.
    relay_satellite.setattr(settings, "streamdeck_ha_base_url", "http://ha.example:8123",
                            raising=False)
    relay_satellite.setattr(settings, "streamdeck_ha_token", "old-ha-token-xyz", raising=False)

    def no_fetch(*a, **k):
        raise AssertionError("a relayed camera is not fetched here")

    relay_satellite.setattr(egress, "guarded_async_client", no_fetch)
    out = await ui_router.camera_diag()
    assert out["ha_connected"] is True and out["ha_via_main_server"] is True
    assert out["cameras"][0]["relayed"] is True
    assert "fetch_status" not in out["cameras"][0]
    assert "old-ha-token-xyz" not in json.dumps(out)
