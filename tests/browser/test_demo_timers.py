"""A finished timer can be cleared on the public demo (FoodAssistant-fy6kj).

The demo refuses every change, and dismissing a timer deletes it from the
registry every screen shares, so the dismiss was refused and the "done" chip
stayed on every page for a day. lib/demo-timers.js (loaded only in demo mode)
hides it from the visitor's own view instead. The suite's app is not in demo
mode, so the demo script is injected and the demo's refusal is answered by a
route, exactly as the gate in main.py answers it. The server's timer must
survive: nothing is written.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

_DEMO_JS = (Path(__file__).resolve().parents[2]
            / "service/app/static/js/lib/demo-timers.js").read_text(encoding="utf-8")

# Run the page's clock two minutes ahead, so a timer that just finished reads
# as finished well past the demo's one-minute linger.
_SKEW = "(() => { const n = Date.now; Date.now = () => n() + 120000; })();"


def _refuse_deletes(route):
    if route.request.method != "DELETE":
        return route.continue_()
    return route.fulfill(
        status=403, headers={"X-Demo-Read-Only": "1"}, content_type="application/json",
        body=json.dumps({"error": "demo_read_only"}))


def _finished_timer(request_ctx, base, label):
    request_ctx.delete(base + "/timers")
    assert request_ctx.post(base + "/timers", data={"label": label, "seconds": 1}).ok
    time.sleep(1.2)


def _page(browser, base, *, demo, skew=False):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    if skew:
        ctx.add_init_script(_SKEW)
    if demo:
        ctx.add_init_script(_DEMO_JS)
        ctx.route("**/timers**", _refuse_deletes)
    return ctx


def test_dismiss_on_the_demo_hides_the_timer_without_deleting_it(browser, app_server):
    ctx = _page(browser, app_server, demo=True)
    try:
        page = ctx.new_page()
        page.goto(app_server + "/ui/timers", wait_until="load")
        _finished_timer(page.request, app_server, "Demo eggs")
        page.reload(wait_until="load")
        page.wait_for_selector("#timersGrid .timer-expired .timer-cancel", timeout=8000)
        page.click("#timersGrid .timer-expired .timer-cancel")
        page.wait_for_selector("#timersEmpty:not(.d-none)", timeout=5000)
        # Still hidden after a reload in the same tab.
        page.reload(wait_until="load")
        page.wait_for_timeout(1500)
        assert page.locator("#timersGrid .timer-card").count() == 0
        # The server kept it: the dismiss wrote nothing.
        labels = [t["label"] for t in page.request.get(app_server + "/timers").json()["timers"]]
        assert "Demo eggs" in labels
        page.request.delete(app_server + "/timers")
    finally:
        ctx.close()


@pytest.mark.parametrize("demo", [True, False])
def test_finished_chip_clears_itself_only_on_the_demo(browser, app_server, demo):
    ctx = _page(browser, app_server, demo=demo, skew=True)
    try:
        page = ctx.new_page()
        _finished_timer(page.request, app_server, "Old pasta")
        page.goto(app_server + "/ui/inventory", wait_until="load")
        if demo:
            page.wait_for_timeout(2500)
            assert page.locator("#timerChips .timer-chip").count() == 0
        else:
            # A normal install keeps the done chip until someone dismisses it.
            page.wait_for_selector("#timerChips .timer-chip-expired", timeout=8000)
            assert page.locator("#timerChips .timer-chip-clock").inner_text() == "done"
        page.request.delete(app_server + "/timers")
    finally:
        ctx.close()
