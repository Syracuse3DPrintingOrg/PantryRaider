"""Kiosk-mode cursor warning (FoodAssistant-8iwj).

Turning kiosk mode on hides the mouse cursor (kiosk.css sets cursor:none),
which surprises anyone who flips it on from a normal browser. The nav-bar
toggle must warn at enable time and tell the user how to get the cursor back.
"""
from __future__ import annotations

from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
BASE_HTML = (SERVICE / "app" / "templates" / "base.html").read_text()


def _toggle_body() -> str:
    """The body of the toggleKioskMode() function, for scoped assertions."""
    start = BASE_HTML.index("function toggleKioskMode()")
    # First save after the guard reassigns the flag and reloads; grab a
    # generous slice that covers the whole function.
    return BASE_HTML[start:start + 1200]


def test_warning_is_shown_on_the_kiosk_toggle():
    body = _toggle_body()
    # A confirm the user must acknowledge, gated on enabling (not disabling).
    assert "window.confirm(" in body
    assert "if (!active) {" in body
    # Warns that the cursor is hidden.
    assert "hides the mouse cursor" in body


def test_warning_says_how_to_get_the_cursor_back():
    body = _toggle_body()
    # The way back is the same nav-bar tablet toggle: there is no exit hotkey.
    assert "bring the cursor back" in body
    assert "tablet icon" in body
    assert "turn kiosk mode off" in body


def test_warning_only_fires_when_enabling():
    body = _toggle_body()
    # The confirm sits inside the enable branch, so disabling never prompts.
    enable_branch = body.index("if (!active) {")
    latch = body.index("localStorage.setItem('kioskMode'")
    assert enable_branch < latch
    assert body.index("window.confirm(") < latch
