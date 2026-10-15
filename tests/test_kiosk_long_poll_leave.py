"""A page's held requests must not outlive the page.

A browser keeps a page it has just left in its back/forward cache with its
requests still running and allows six connections to one site, so each page's
25-second events request used to pile up until the next page waited for a free
connection: a kiosk froze for 15 to 25 seconds after a few quick taps (measured
on a Raspberry Pi kiosk, 2026-10-15). Both long-held requests are cancelled when
the page is being left, and the server ends a held request once the browser has
gone.
"""
import asyncio
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "service"))

from app.services import ha_events  # noqa: E402

JS = ROOT / "service" / "app" / "static" / "js"


def _code(name):
    text = (JS / name).read_text()
    return re.sub(r"//[^\n]*", "", text)


def test_events_long_poll_is_cancelled_when_the_page_is_left():
    js = _code("ha-events.js")
    assert "AbortController" in js
    for event in ("beforeunload", "pagehide", "pageshow"):
        assert re.search(r"addEventListener\('%s'" % event, js), event
    # The fetch carries the abort signal, and an aborted request does not
    # start another one.
    assert re.search(r"fetch\('events/poll\?wait='[^;]*opts\)", js)
    assert "signal.aborted" in js


def test_status_poll_is_cancelled_when_the_page_is_left():
    js = _code("kiosk-status.js")
    for event in ("beforeunload", "pagehide", "pageshow"):
        assert re.search(r"addEventListener\('%s'" % event, js), event
    assert "current.abort()" in js


def test_held_wait_ends_when_the_client_is_gone(monkeypatch):
    monkeypatch.setattr(ha_events, "poll", lambda after: {"events": [], "last_id": after})
    calls = {"n": 0}

    async def gone():
        calls["n"] += 1
        return calls["n"] >= 2

    t0 = time.monotonic()
    out = asyncio.run(ha_events.wait_for_events(5, timeout=20, interval=0.02, is_gone=gone))
    assert out == {"events": [], "last_id": 5}
    assert time.monotonic() - t0 < 3


def test_held_wait_ignores_a_wall_clock_jump(monkeypatch):
    # A Pi's clock jumps when it reaches a time server after boot; the wait is
    # measured on the monotonic clock, so a jump neither ends it nor stretches it.
    monkeypatch.setattr(ha_events, "poll", lambda after: {"events": [], "last_id": after})
    real = time.time
    monkeypatch.setattr(ha_events.time, "time", lambda: real() + 3600)
    t0 = time.monotonic()
    asyncio.run(ha_events.wait_for_events(0, timeout=0.3, interval=0.02))
    assert 0.25 <= time.monotonic() - t0 < 2
