"""Start Page timer keys render a live face after a fire (FoodAssistant-uzra).

The booted app's real /timers registry is the source of truth: starting the
shared timer (here through the key press itself, the same ui/start/fire path a
finger takes) must turn the matching Start Page key into a live countdown
face, and an expired timer must flip the key to its pulsing Done state.
"""
from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.browser


def test_timer_key_shows_live_countdown_after_fire(new_page, app_server):
    page = new_page("/ui/start")
    page.request.delete(app_server + "/timers")
    key = page.locator('.start-key[data-key="timer_1"]')
    assert key.count() == 1

    # Press the key: it fires server-side and starts the shared "Timer 1".
    key.click()
    page.wait_for_selector('.start-key[data-key="timer_1"].timer-running .countdown',
                           timeout=8000)
    face = page.locator('.start-key[data-key="timer_1"] .countdown').inner_text()
    assert re.fullmatch(r"\d+:\d\d", face), f"unexpected face {face!r}"

    # The shared registry really has it (the key adopted a real timer, not a
    # local animation).
    data = page.request.get(app_server + "/timers").json()
    assert "Timer 1" in [t.get("label") for t in data.get("timers", [])]
    page.request.delete(app_server + "/timers")


def test_timer_key_pulses_done_after_expiry(new_page, app_server):
    page = new_page("/ui/start")
    page.request.delete(app_server + "/timers")
    # A one-second timer through the real registry; the page polls every 3s
    # and ticks locally, so the key should flip to Done shortly after expiry.
    r = page.request.post(app_server + "/timers",
                          data={"label": "Eggs", "seconds": 1})
    assert r.ok
    page.wait_for_selector('.start-key[data-key="timer_eggs"].timer-done',
                           timeout=10000)
    face = page.locator('.start-key[data-key="timer_eggs"] .countdown').inner_text()
    assert face == "Done"
    page.request.delete(app_server + "/timers")
