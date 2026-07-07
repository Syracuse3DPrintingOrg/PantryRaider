"""Tests for camera feed resolution (service/app/services/cameras.py).

Home Assistant cameras must be fetched with a bearer header, not the long-lived
token in the query string, so they are proxied by the app. These cover the
entity resolution (including recovery from a legacy token-baked URL) and the
view model the kiosk page renders.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import cameras  # noqa: E402


@pytest.fixture(autouse=True)
def _ha(monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "http://ha.local:8123", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "tok", raising=False)
    yield


def test_resolve_entity_from_explicit_field():
    entity, base = cameras.resolve_ha_entity({"ha_entity": "camera.front_door"})
    assert entity == "camera.front_door"
    assert base == "http://ha.local:8123"


def test_resolve_entity_recovered_from_legacy_url():
    # A camera saved before entity-based proxying: the entity is parsed back out
    # of the (non-working) token-baked URL so it still resolves.
    entry = {"snapshot_url": "http://ha.local:8123/api/camera_proxy/camera.garage?token=LLAT"}
    entity, base = cameras.resolve_ha_entity(entry)
    assert entity == "camera.garage"
    assert base == "http://ha.local:8123"


def test_ha_feed_builds_bearer_url():
    url, headers = cameras.ha_feed({"ha_entity": "camera.x"}, "snapshot")
    assert url == "http://ha.local:8123/api/camera_proxy/camera.x"
    assert headers == {"Authorization": "Bearer tok"}
    surl, _ = cameras.ha_feed({"ha_entity": "camera.x"}, "stream")
    assert surl == "http://ha.local:8123/api/camera_proxy_stream/camera.x"


def test_ha_feed_none_for_plain_camera():
    url, headers = cameras.ha_feed({"snapshot_url": "http://192.168.1.5/snap.jpg"}, "snapshot")
    assert url is None and headers is None


def test_camera_sources_view_model():
    cams = [
        {"name": "Door", "ha_entity": "camera.door"},
        {"name": "Shed", "snapshot_url": "http://1.2.3.4/s.jpg", "stream_url": "http://1.2.3.4/v"},
    ]
    out = cameras.camera_sources(cams)
    assert out[0]["is_ha"] is True
    assert out[0]["stream_src"] == "ui/camera/0/stream"
    assert out[0]["snapshot_src"] == "ui/camera/0/snapshot"
    assert out[1]["is_ha"] is False
    assert out[1]["stream_src"] == "http://1.2.3.4/v"


# -- resolve_camera_index (FoodAssistant-f230) ------------------------------

_CAMS = [
    {"name": "Front Door", "ha_entity": "camera.front"},
    {"name": "Garage", "ha_entity": "camera.garage"},
    {"name": "Shed", "snapshot_url": "http://1.2.3.4/s.jpg"},
]


def test_resolve_camera_index_by_name_case_insensitive():
    assert cameras.resolve_camera_index(_CAMS, "garage") == 1
    assert cameras.resolve_camera_index(_CAMS, "  Shed ") == 2


def test_resolve_camera_index_by_position():
    assert cameras.resolve_camera_index(_CAMS, "2") == 2


def test_resolve_camera_index_defaults_to_first():
    # Empty, unknown name, and out-of-range index all fall back to camera 0.
    assert cameras.resolve_camera_index(_CAMS, "") == 0
    assert cameras.resolve_camera_index(_CAMS, "nope") == 0
    assert cameras.resolve_camera_index(_CAMS, "9") == 0


def test_resolve_camera_index_empty_list():
    assert cameras.resolve_camera_index([], "Garage") == 0


# -- Reolink (FoodAssistant-qft4) -------------------------------------------
# A Reolink entry keeps its login in settings; the URLs are composed server-side
# and the snapshot is always proxied, so no credential reaches a browser field.

_REOLINK = {
    "name": "Doorbell", "source": "reolink", "host": "192.168.1.60",
    "channel": 0, "username": "admin", "password": "s3cret",
}


def test_reolink_snapshot_url_composes_cgi_with_login():
    url = cameras.reolink_snapshot_from_entry(_REOLINK)
    assert url == ("http://192.168.1.60/cgi-bin/api.cgi?cmd=Snap&channel=0"
                   "&rs=foodassistant&user=admin&password=s3cret")


def test_reolink_rtsp_url_is_channel_one_based():
    # channel 0 maps to the 1-based h264Preview_01 path; the login is embedded.
    assert cameras.reolink_rtsp_from_entry(_REOLINK) == \
        "rtsp://admin:s3cret@192.168.1.60:554/h264Preview_01_main"
    sub = {**_REOLINK, "channel": 1, "stream_quality": "sub"}
    assert cameras.reolink_rtsp_from_entry(sub) == \
        "rtsp://admin:s3cret@192.168.1.60:554/h264Preview_02_sub"


def test_reolink_url_encodes_credentials():
    entry = {**_REOLINK, "username": "user name", "password": "p@ss/word"}
    snap = cameras.reolink_snapshot_from_entry(entry)
    assert "user=user%20name" in snap and "password=p%40ss%2Fword" in snap


def test_reolink_source_view_model_hides_credentials():
    # The page-facing view model routes a Reolink camera through the app proxy and
    # never exposes the credentialed URL in any field.
    out = cameras.camera_sources([_REOLINK])
    assert out[0]["snapshot_src"] == "ui/camera/0/snapshot"
    assert out[0]["stream_src"] == ""
    assert out[0]["is_ha"] is False
    blob = str(out)
    assert "s3cret" not in blob and "admin" not in blob


def test_proxied_snapshot_does_not_resolve_reolink_statically():
    # A Reolink camera now needs a two-step token sign-in, so it is fetched
    # through fetch_reolink_snapshot rather than resolved to a static URL here.
    assert cameras.proxied_snapshot(_REOLINK) == (None, None)


def test_proxied_snapshot_covers_plain_manual_camera():
    # A manual/Frigate camera carries no secret but is still fetched server-side
    # so an http camera works on an https page (FoodAssistant-p1w5).
    url, headers = cameras.proxied_snapshot({"snapshot_url": "http://1.2.3.4/s.jpg"})
    assert url == "http://1.2.3.4/s.jpg" and headers is None


def test_proxied_snapshot_still_handles_ha_bearer():
    # Existing HA cameras are unaffected: still proxied with the bearer header.
    url, headers = cameras.proxied_snapshot({"ha_entity": "camera.x"})
    assert url == "http://ha.local:8123/api/camera_proxy/camera.x"
    assert headers == {"Authorization": "Bearer tok"}


# FoodAssistant-p1w5: pasted-URL host + server-side proxy for manual/Frigate.
def test_reolink_host_accepts_pasted_url_scheme():
    from app.services.cameras import reolink_snapshot_url, reolink_rtsp_url
    snap = reolink_snapshot_url("https://192.168.1.221", channel=0,
                                username="u", password="p", port="80")
    assert snap.startswith("http://192.168.1.221:80/cgi-bin/api.cgi")
    assert "https://" not in snap.split("?")[0]  # no http://https:// authority
    rtsp = reolink_rtsp_url("http://192.168.1.221", channel=0,
                            username="u", password="p")
    assert rtsp.startswith("rtsp://u:p@192.168.1.221:554/")


def test_proxied_snapshot_covers_manual_and_frigate():
    from app.services.cameras import proxied_snapshot
    url, headers = proxied_snapshot(
        {"name": "Door", "snapshot_url": "http://10.0.0.5:5000/api/Door/latest.jpg"})
    assert url == "http://10.0.0.5:5000/api/Door/latest.jpg"
    assert headers is None
    # A camera with no fetchable snapshot still returns nothing to proxy.
    assert proxied_snapshot({"name": "x"}) == (None, None)


# -- Reolink token sign-in (FoodAssistant-t893) -----------------------------
# Newer Reolink firmware rejects inline user/password on the Snap CGI. The app
# now signs in for a short-lived token and fetches the still with that token.
# All of these mock httpx; nothing touches the network.

import httpx  # noqa: E402

_JPEG = b"\xff\xd8\xff\xe0jpegbytes"


@pytest.fixture(autouse=True)
def _clear_reolink_tokens():
    cameras._reolink_tokens.clear()
    yield
    cameras._reolink_tokens.clear()


def _login_ok(lease=3600):
    return httpx.Response(200, json=[{"cmd": "Login", "code": 0,
        "value": {"Token": {"name": "TOK123", "leaseTime": lease}}}])


def _login_bad():
    return httpx.Response(200, json=[{"cmd": "Login", "code": 1,
        "error": {"detail": "login failed", "rspCode": -6}}])


def _snap_ok():
    return httpx.Response(200, content=_JPEG,
                          headers={"content-type": "image/jpeg"})


def _mock_httpx(monkeypatch, handler):
    """Route every httpx.AsyncClient in cameras through a MockTransport."""
    orig = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("verify", None)
        kwargs["transport"] = httpx.MockTransport(handler)
        return orig(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


@pytest.mark.anyio
async def test_reolink_token_login_then_snap_returns_bytes(monkeypatch):
    calls = {"login": 0, "snap": 0}

    def handler(request):
        q = request.url.query.decode()
        if "cmd=Login" in q:
            calls["login"] += 1
            body = request.content.decode()
            assert "admin" in body and "s3cret" in body
            return _login_ok()
        if "cmd=Snap" in q:
            calls["snap"] += 1
            assert "token=TOK123" in q
            return _snap_ok()
        return httpx.Response(404)

    _mock_httpx(monkeypatch, handler)
    status, content, ctype = await cameras.fetch_reolink_snapshot(_REOLINK)
    assert status == 200 and content == _JPEG and ctype.startswith("image/")
    assert calls == {"login": 1, "snap": 1}


@pytest.mark.anyio
async def test_reolink_login_failure_raises_auth_error(monkeypatch):
    def handler(request):
        if "cmd=Login" in request.url.query.decode():
            return _login_bad()
        return _snap_ok()

    _mock_httpx(monkeypatch, handler)
    with pytest.raises(cameras.ReolinkAuthError):
        await cameras.fetch_reolink_snapshot(_REOLINK)
    # A failed sign-in leaves no token cached.
    assert not cameras._reolink_tokens


@pytest.mark.anyio
async def test_reolink_login_401_raises_auth_error(monkeypatch):
    def handler(request):
        if "cmd=Login" in request.url.query.decode():
            return httpx.Response(401)
        return _snap_ok()

    _mock_httpx(monkeypatch, handler)
    with pytest.raises(cameras.ReolinkAuthError):
        await cameras.fetch_reolink_snapshot(_REOLINK)


@pytest.mark.anyio
async def test_reolink_token_is_cached_between_snapshots(monkeypatch):
    calls = {"login": 0, "snap": 0}

    def handler(request):
        q = request.url.query.decode()
        if "cmd=Login" in q:
            calls["login"] += 1
            return _login_ok()
        calls["snap"] += 1
        return _snap_ok()

    _mock_httpx(monkeypatch, handler)
    await cameras.fetch_reolink_snapshot(_REOLINK)
    await cameras.fetch_reolink_snapshot(_REOLINK)
    # A second snapshot within the lease reuses the cached token, no re-login.
    assert calls == {"login": 1, "snap": 2}


@pytest.mark.anyio
async def test_reolink_relogins_after_a_401_snap(monkeypatch):
    calls = {"login": 0, "snap": 0}

    def handler(request):
        q = request.url.query.decode()
        if "cmd=Login" in q:
            calls["login"] += 1
            return _login_ok()
        calls["snap"] += 1
        # First snap: token stale (401); after re-login the snap succeeds.
        if calls["snap"] == 1:
            return httpx.Response(401, text="please login")
        return _snap_ok()

    _mock_httpx(monkeypatch, handler)
    status, content, _ = await cameras.fetch_reolink_snapshot(_REOLINK)
    assert status == 200 and content == _JPEG
    assert calls["login"] == 2 and calls["snap"] == 2


@pytest.mark.anyio
async def test_reolink_relogins_when_token_expired(monkeypatch):
    calls = {"login": 0}

    def handler(request):
        q = request.url.query.decode()
        if "cmd=Login" in q:
            calls["login"] += 1
            return _login_ok()
        return _snap_ok()

    _mock_httpx(monkeypatch, handler)
    # Seed an already-expired token; the fetcher must sign in again.
    cameras._reolink_tokens[cameras._reolink_authority(_REOLINK["host"], "")] = (
        "OLD", 0.0)
    await cameras.fetch_reolink_snapshot(_REOLINK)
    assert calls["login"] == 1


@pytest.mark.anyio
async def test_add_reolink_camera_verify_ok_on_good_login(monkeypatch):
    from app.routers import setup as setup_router

    async def fake_fetch(entry, timeout=8.0):
        return 200, _JPEG, "image/jpeg"

    monkeypatch.setattr(cameras, "fetch_reolink_snapshot", fake_fetch)
    saved = {}
    monkeypatch.setattr(type(settings), "save", lambda self, d: saved.update(d))
    monkeypatch.setattr(settings, "streamdeck_cameras", [], raising=False)
    payload = setup_router.ReolinkAddPayload(
        name="Doorbell", host="192.168.1.60", username="admin", password="s3cret")
    result = await setup_router.add_reolink_camera(payload)
    assert result["ok"] is True
    # The password is never handed back to the page.
    assert "password" not in result["camera"]
    assert saved["streamdeck_cameras"][0]["source"] == "reolink"


@pytest.mark.anyio
async def test_add_reolink_camera_verify_rejects_bad_login(monkeypatch):
    from app.routers import setup as setup_router

    async def fake_fetch(entry, timeout=8.0):
        raise cameras.ReolinkAuthError("rejected")

    monkeypatch.setattr(cameras, "fetch_reolink_snapshot", fake_fetch)
    payload = setup_router.ReolinkAddPayload(
        host="192.168.1.60", username="admin", password="wrong")
    result = await setup_router.add_reolink_camera(payload)
    assert result["ok"] is False
    assert "rejected that username or password" in result["error"]


@pytest.mark.anyio
async def test_camera_snapshot_serves_reolink_via_token_flow(monkeypatch):
    from app.routers import ui as ui_router

    async def fake_fetch(entry, timeout=8.0):
        return 200, _JPEG, "image/jpeg"

    monkeypatch.setattr(cameras, "fetch_reolink_snapshot", fake_fetch)
    monkeypatch.setattr(ui_router.settings, "streamdeck_cameras", [_REOLINK],
                        raising=False)
    resp = await ui_router.camera_snapshot(0)
    assert resp.status_code == 200
    assert resp.body == _JPEG
    assert resp.media_type.startswith("image/")


@pytest.mark.anyio
async def test_camera_snapshot_reolink_reports_bad_login(monkeypatch):
    from app.routers import ui as ui_router

    async def fake_fetch(entry, timeout=8.0):
        raise cameras.ReolinkAuthError("rejected")

    monkeypatch.setattr(cameras, "fetch_reolink_snapshot", fake_fetch)
    monkeypatch.setattr(ui_router.settings, "streamdeck_cameras", [_REOLINK],
                        raising=False)
    resp = await ui_router.camera_snapshot(0)
    assert resp.status_code == 502
    assert b"rejected that username or password" in resp.body


@pytest.mark.anyio
async def test_camera_snapshot_non_reolink_untouched(monkeypatch):
    # A manual camera still redirects to its own snapshot URL; the token flow is
    # never invoked for it.
    from app.routers import ui as ui_router

    async def boom(entry, timeout=8.0):
        raise AssertionError("token flow must not run for a manual camera")

    monkeypatch.setattr(cameras, "fetch_reolink_snapshot", boom)
    # A camera with no fetchable snapshot exercises the non-Reolink branch with
    # no network: it reports "no snapshot" and never touches the token flow.
    monkeypatch.setattr(
        ui_router.settings, "streamdeck_cameras", [{"name": "Door"}], raising=False)
    resp = await ui_router.camera_snapshot(0)
    assert resp.status_code == 404
