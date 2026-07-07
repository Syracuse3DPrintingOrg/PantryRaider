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


def test_proxied_snapshot_composes_reolink_url_server_side():
    # The proxy resolves the Reolink entry to the credentialed upstream (fetched
    # server-side), with no auth header (the login is in the URL).
    url, headers = cameras.proxied_snapshot(_REOLINK)
    assert url == cameras.reolink_snapshot_from_entry(_REOLINK)
    assert headers is None


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
