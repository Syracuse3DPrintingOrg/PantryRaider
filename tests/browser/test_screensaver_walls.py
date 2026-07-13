"""Screensaver bounce walls under kiosk zoom and rotation (FoodAssistant-vf4f).

The kiosk interface scale applies as an html zoom, and the bounce math once
mixed unzoomed viewport pixels with zoomed transform coordinates, so the logo
reflected short of (or past) the right and bottom walls; a rotated (portrait)
panel made it obvious. These tests force the saver with a fast test speed and
sample the logo block's on-screen box every frame: it must never leave the
viewport and must actually reach every wall.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

# Reads the logo block's box in the same coordinate space as the window
# (getBoundingClientRect reflects CSS zoom in Chromium), sampling per frame.
_SAMPLE_JS = """
async (durationMs) => {
  const block = document.querySelector('#kiosk-screensaver .ss-block');
  const res = { minLeft: 1e9, maxRight: -1e9, minTop: 1e9, maxBottom: -1e9,
                vw: window.innerWidth, vh: window.innerHeight, samples: 0 };
  const t0 = performance.now();
  return await new Promise((resolve) => {
    function tick() {
      const r = block.getBoundingClientRect();
      res.minLeft = Math.min(res.minLeft, r.left);
      res.maxRight = Math.max(res.maxRight, r.right);
      res.minTop = Math.min(res.minTop, r.top);
      res.maxBottom = Math.max(res.maxBottom, r.bottom);
      res.samples += 1;
      if (performance.now() - t0 < durationMs) requestAnimationFrame(tick);
      else resolve(res);
    }
    requestAnimationFrame(tick);
  });
}
"""

# Fast enough to cross the screen a few times inside the sample window without
# skipping more than ~25 layout px between frames at 60 fps.
_SPEED_PX = 1500
_SAMPLE_MS = 4000
# The block must never be drawn past a wall (small allowance for zoom
# rounding), and must have kissed each wall at least once while sampled.
_OVERSHOOT = 3
_REACH = 45


@pytest.mark.parametrize("zoom", [0.85, 1.25])
@pytest.mark.parametrize("viewport", [(1280, 800), (600, 1024)],
                         ids=["landscape", "portrait"])
def test_bounce_walls_stay_true_under_zoom(new_page, zoom, viewport):
    page = new_page("/ui/inventory", kiosk=True, viewport=viewport, zoom=zoom)
    page.evaluate("window.__screensaverTest({ speedPx: %d })" % _SPEED_PX)
    page.wait_for_selector("#kiosk-screensaver .ss-block", timeout=5000)
    res = page.evaluate(_SAMPLE_JS, _SAMPLE_MS)

    assert res["samples"] > 30, "the sampling loop barely ran"
    vw, vh = res["vw"], res["vh"]
    # Never drawn outside the viewport on any side.
    assert res["minLeft"] >= -_OVERSHOOT
    assert res["minTop"] >= -_OVERSHOOT
    assert res["maxRight"] <= vw + _OVERSHOOT
    assert res["maxBottom"] <= vh + _OVERSHOOT
    # Actually reaches every wall (the vf4f bug stopped short of right/bottom
    # by the zoom factor, which this catches at both zooms and rotations).
    assert res["minLeft"] <= _REACH
    assert res["minTop"] <= _REACH
    assert res["maxRight"] >= vw - _REACH
    assert res["maxBottom"] >= vh - _REACH
