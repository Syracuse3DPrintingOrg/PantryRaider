"""The camera page refreshes at a sane pace and stops while hidden
(FoodAssistant-fy6kj).

A camera still can run past a megabyte, and the page used to pull one every
1.5 s, hidden tab or not. The real page script runs here against a routed
stand-in for the app (camera.html's elements, a snapshot route that counts
its requests), and the tab's visibility is switched by hand.
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

_JS = Path(__file__).resolve().parents[2] / "service/app/static/js"
_ORIGIN = "http://camera.test"
# A 1x1 PNG.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")

_PAGE = """<!doctype html><html><head><base href="/">
<script src="static/js/lib/dom.js"></script></head><body>
<div id="cameraStage">
  <video id="cameraVideo" class="d-none"></video>
  <img id="cameraImg" class="d-none" alt="Camera feed">
  <div id="cameraError" class="d-none"><div id="cameraErrorMsg"></div></div>
</div>
<script type="application/json" id="camera-data">%s</script>
<script src="static/js/pages/camera.js"></script>
</body></html>""" % json.dumps({"cameras": [{"name": "Door"}], "initial_index": 0})

# Lets the test hide and show the tab the way the browser would.
_VISIBILITY = """
window.__hidden = false;
Object.defineProperty(document, 'hidden', { get: () => window.__hidden });
Object.defineProperty(document, 'visibilityState',
  { get: () => (window.__hidden ? 'hidden' : 'visible') });
window.__setHidden = (h) => {
  window.__hidden = h;
  document.dispatchEvent(new Event('visibilitychange'));
};
"""


def test_interval_is_not_the_old_one_and_a_half_seconds():
    js = (_JS / "pages/camera.js").read_text(encoding="utf-8")
    ms = int(re.search(r"const SNAPSHOT_INTERVAL_MS = (\d+);", js).group(1))
    assert 2500 <= ms <= 5000, ms


def _wait_for(page, cond, ms):
    for _ in range(ms // 100):
        if cond():
            return
        page.wait_for_timeout(100)
    assert cond()


def test_snapshot_refresh_pauses_while_hidden(browser):
    hits = []

    def handle(route):
        path = route.request.url.split(_ORIGIN, 1)[1].split("?", 1)[0]
        if path == "/ui/camera":
            return route.fulfill(content_type="text/html", body=_PAGE)
        if path.startswith("/static/js/"):
            body = (_JS / path[len("/static/js/"):]).read_text(encoding="utf-8")
            return route.fulfill(content_type="text/javascript", body=body)
        if re.fullmatch(r"/ui/camera/0/snapshot", path):
            hits.append(path)
            return route.fulfill(content_type="image/png", body=_PNG)
        return route.fulfill(status=404, body="")

    ctx = browser.new_context()
    try:
        ctx.add_init_script(_VISIBILITY)
        ctx.route(_ORIGIN + "/**", handle)
        page = ctx.new_page()
        page.goto(_ORIGIN + "/ui/camera", wait_until="load")
        page.wait_for_timeout(3500)
        visible = len(hits)
        # One on load and one more a refresh later, never the old pace of a
        # frame every 1.5 s (three or four in this window).
        assert 1 <= visible <= 2, visible

        page.evaluate("window.__setHidden(true)")
        page.wait_for_timeout(7000)
        assert len(hits) == visible, "a hidden tab kept pulling snapshots"

        # Back in view: a fresh frame straight away, then the refresh goes on.
        page.evaluate("window.__setHidden(false)")
        _wait_for(page, lambda: len(hits) >= visible + 1, 2000)
        assert len(hits) == visible + 1
        _wait_for(page, lambda: len(hits) >= visible + 2, 6000)
    finally:
        ctx.close()
