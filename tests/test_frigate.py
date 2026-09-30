"""Tests for Frigate camera discovery (service/app/services/frigate.py).

Frigate lists its cameras at GET /api/config; each camera has a still at
/api/<name>/latest.jpg. These cover the config parse, the URL builders, and the
soft-fail behaviour when Frigate is unreachable or lists nothing. All HTTP is
mocked; no network.
"""
from __future__ import annotations

import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import frigate  # noqa: E402


class _Resp:
    def __init__(self, status_code=200, payload=None, raise_json=False):
        self.status_code = status_code
        self._payload = payload
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


def test_normalize_base_defaults_scheme_and_trims():
    assert frigate.normalize_base("frigate.local:5000") == "http://frigate.local:5000"
    assert frigate.normalize_base("http://f.local:5000/") == "http://f.local:5000"
    assert frigate.normalize_base("  ") == ""


def test_url_builders():
    assert frigate.snapshot_url("http://f.local:5000", "driveway") == \
        "http://f.local:5000/api/driveway/latest.jpg"
    assert frigate.stream_url("frigate.local:5000", "driveway") == \
        "http://frigate.local:5000/api/driveway"
    # No host or no name yields no URL.
    assert frigate.snapshot_url("", "x") == ""
    assert frigate.snapshot_url("http://f", "") == ""


def test_parse_config_reads_camera_map():
    cfg = {"cameras": {"front": {}, "driveway": {}}, "mqtt": {}}
    assert sorted(frigate.parse_config(cfg)) == ["driveway", "front"]


def test_parse_config_soft_on_bad_shapes():
    assert frigate.parse_config(None) == []
    assert frigate.parse_config({"cameras": []}) == []
    assert frigate.parse_config({"no_cameras": {}}) == []


def test_discover_success_builds_entries():
    cfg = {"cameras": {"front_door": {}, "yard": {}}}
    result = frigate.discover("frigate.local:5000",
                              fetch=lambda url: _Resp(200, cfg))
    assert result["ok"] is True
    assert result["base_url"] == "http://frigate.local:5000"
    names = [c["name"] for c in result["cameras"]]
    assert names == ["front_door", "yard"]   # sorted
    front = result["cameras"][0]
    assert front["snapshot_url"] == "http://frigate.local:5000/api/front_door/latest.jpg"
    assert front["stream_url"] == "http://frigate.local:5000/api/front_door"


def test_discover_soft_fails_when_unreachable():
    def _boom(url):
        raise ConnectionError("refused")
    result = frigate.discover("http://frigate.local:5000", fetch=_boom)
    assert result["ok"] is False
    assert "Could not reach Frigate" in result["error"]


def test_discover_soft_fails_on_empty_camera_list():
    result = frigate.discover("http://frigate.local:5000",
                              fetch=lambda url: _Resp(200, {"cameras": {}}))
    assert result["ok"] is False
    assert "No cameras found" in result["error"]


def test_discover_soft_fails_on_http_error():
    result = frigate.discover("http://frigate.local:5000",
                              fetch=lambda url: _Resp(404, None))
    assert result["ok"] is False
    assert "HTTP 404" in result["error"]


def test_discover_needs_an_address():
    result = frigate.discover("")
    assert result["ok"] is False
    assert "Frigate address" in result["error"]
