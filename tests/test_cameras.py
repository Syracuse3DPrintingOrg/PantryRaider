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
