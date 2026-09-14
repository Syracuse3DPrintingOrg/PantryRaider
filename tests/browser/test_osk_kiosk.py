"""On-screen keyboard behavior in a real browser (FoodAssistant-wo9j).

Typing goes through the actual key buttons, so the caret handling, the input
events, and the Enter-submits-the-form path are exercised the way a kiosk
touch would, on the real Timers page form.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


def _tap(page, selector):
    page.locator(f"#pr-osk {selector}").click()


def test_osk_types_and_enter_submits(new_page, app_server):
    page = new_page("/ui/timers", kiosk=True)
    page.request.delete(app_server + "/timers")  # a clean registry

    # Focusing a text input slides the keyboard up.
    page.click("#timerCustomLabel")
    page.wait_for_selector("#pr-osk.pr-osk-open", timeout=5000)

    # Tap out a label on the letter keys; the input fills like real typing.
    for ch in ("e", "g", "g", "s"):
        _tap(page, f'button[data-osk-char="{ch}"]')
    assert page.input_value("#timerCustomLabel") == "eggs"

    # A number input swaps to the digits-only pad.
    page.click("#timerCustomMinutes")
    page.wait_for_selector("#pr-osk.pr-osk-number", timeout=5000)
    _tap(page, 'button[data-osk-char="2"]')
    assert page.input_value("#timerCustomMinutes") == "2"

    # Enter mirrors a real Enter press: the form submits and the shared timer
    # registry (the real /timers API on the booted app) gains the timer.
    page.click("#timerCustomLabel")
    page.wait_for_selector("#pr-osk.pr-osk-open", timeout=5000)
    _tap(page, 'button[data-osk-action="ENTER"]')

    def timer_labels():
        data = page.request.get(app_server + "/timers").json()
        return [t.get("label") for t in data.get("timers", [])]

    page.wait_for_timeout(300)
    deadline = 20
    while "eggs" not in timer_labels() and deadline:
        page.wait_for_timeout(250)
        deadline -= 1
    assert "eggs" in timer_labels()
    page.request.delete(app_server + "/timers")


def test_osk_shift_uppercases_one_shot(new_page):
    page = new_page("/ui/timers", kiosk=True)
    page.click("#timerCustomLabel")
    page.wait_for_selector("#pr-osk.pr-osk-open", timeout=5000)
    _tap(page, 'button[data-osk-action="SHIFT"]')
    _tap(page, 'button[data-osk-char="p"]')  # one-shot shift spends itself
    _tap(page, 'button[data-osk-char="i"]')
    assert page.input_value("#timerCustomLabel") == "Pi"


def test_osk_absent_outside_kiosk_mode(new_page):
    page = new_page("/ui/timers", kiosk=False)
    page.click("#timerCustomLabel")
    page.wait_for_timeout(400)
    assert page.locator("#pr-osk").count() == 0
