"""Tests for the Reolink AI-detection poll pass (FoodAssistant-akd0).

``poll_reolink_cameras_once`` takes its fetcher and event-add function as
injected callables, so it is exercised here without any network or the real
ha_events state file.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services.camera_popup_poll import poll_reolink_cameras_once  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def test_pops_up_reolink_camera_with_enabled_detection():
    cams = [{"name": "Front Door", "source": "reolink", "popup_types": ["person"]}]
    popped_calls = []

    async def fetch(cam):
        return {"value": {"people": {"alarm_state": 1}}}

    def add_camera(**kw):
        popped_calls.append(kw)
        return 1

    popped = _run(poll_reolink_cameras_once(cams, fetch, 20, add_camera))
    assert popped == [0]
    assert popped_calls == [{"name": "Front Door", "src": "ui/camera/0/snapshot", "seconds": 20}]


def test_skips_camera_with_no_popup_types_configured():
    cams = [{"name": "Shed", "source": "reolink"}]
    calls = []

    async def fetch(cam):
        calls.append(cam)
        return {"value": {"people": {"alarm_state": 1}}}

    popped = _run(poll_reolink_cameras_once(cams, fetch, 20, lambda **kw: 1))
    assert popped == []
    assert calls == []  # never even fetched: no types are enabled to check against


def test_skips_non_reolink_cameras():
    cams = [{"name": "HA cam", "ha_entity": "camera.x", "popup_types": ["person"]}]
    popped = _run(poll_reolink_cameras_once(
        cams, lambda cam: asyncio.sleep(0, result={}), 20, lambda **kw: 1))
    assert popped == []


def test_no_popup_when_detected_type_not_enabled():
    cams = [{"name": "Front Door", "source": "reolink", "popup_types": ["animal"]}]

    async def fetch(cam):
        return {"value": {"people": {"alarm_state": 1}}}

    popped = _run(poll_reolink_cameras_once(cams, fetch, 20, lambda **kw: 1))
    assert popped == []


def test_unreachable_camera_is_skipped_not_raised():
    cams = [
        {"name": "Broken", "source": "reolink", "popup_types": ["person"]},
        {"name": "Working", "source": "reolink", "popup_types": ["person"]},
    ]
    calls = []

    async def fetch(cam):
        if cam["name"] == "Broken":
            raise ConnectionError("unreachable")
        return {"value": {"people": {"alarm_state": 1}}}

    def add_camera(**kw):
        calls.append(kw)
        return 1

    popped = _run(poll_reolink_cameras_once(cams, fetch, 20, add_camera))
    assert popped == [1]
    assert len(calls) == 1 and calls[0]["name"] == "Working"


def test_empty_camera_list():
    popped = _run(poll_reolink_cameras_once([], lambda cam: asyncio.sleep(0, result={}), 20, lambda **kw: 1))
    assert popped == []
