"""Kiosk panel guards (FoodAssistant-9k2v).

Two source-level pins for bugs that only show up on real hardware, which is
exactly why they need a guard here:

1. The ui_scale zoom compensation. Legacy CSS zoom scaled painting but not the
   layout viewport, so kiosk-display.js laid the page out at width/scale to
   cancel it. Chromium 128 standardized CSS zoom and now adjusts the viewport
   itself, so that compensation applies twice and inflates the page by 1/scale:
   on the Bandit (Chrome 150, scale 0.85) the root laid out 581px wide inside a
   500px viewport and the right 81px of every settings page sat off the panel.
   The compensation must stay behind a runtime probe, never unconditional.

2. Waking the screensaver by touch must report activity. The screensaver
   swallows the dismissing tap (capture phase + stopPropagation) so it cannot
   press the page underneath, which also hides it from kiosk-idle.js. Without
   an explicit hand-off the panel keeps counting toward display sleep from
   before the screensaver appeared and goes dark seconds after being woken.
"""
import pathlib

JS = pathlib.Path(__file__).resolve().parents[1] / "service/app/static/js"


def test_zoom_width_compensation_is_probed_not_assumed():
    src = (JS / "kiosk-display.js").read_text()
    assert "calc(100% / ' + scale + ')" in src, "the compensation itself went missing"
    # It must sit behind a measurement of how this browser reports zoomed
    # geometry, not run for every browser.
    probe_marker = "100 * scale"
    assert probe_marker in src, (
        "the zoom compensation must be gated on a runtime probe; an "
        "unconditional width/scale double-compensates on Chromium 128+")
    idx_probe = src.index(probe_marker)
    idx_apply = src.index("calc(100% / ' + scale + ')")
    assert idx_probe < idx_apply, "the probe must run before the compensation is applied"


def test_screensaver_reports_activity_when_it_swallows_the_waking_tap():
    src = (JS / "screensaver.js").read_text()
    assert "__prKioskActivity" in src, (
        "waking the screensaver must report activity; the tap is swallowed by "
        "stopPropagation and kiosk-idle.js never sees it, so the display sleep "
        "countdown never restarts")
    # The call has to live in the branch that swallows and hides, before hide().
    idx_call = src.index("__prKioskActivity")
    idx_hide = src.index("hide();", idx_call)
    assert idx_hide > idx_call


def test_kiosk_idle_exposes_the_activity_hook():
    src = (JS / "kiosk-idle.js").read_text()
    assert "window.__prKioskActivity = onActivity" in src, (
        "screensaver.js depends on this hook to report a wake")
