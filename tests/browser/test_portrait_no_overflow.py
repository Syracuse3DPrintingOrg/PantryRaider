"""Every main page fits a portrait kiosk panel with no horizontal overflow.

The Bandit-class appliance drives a 480x800 portrait touchscreen, and pages
that were only ever eyeballed on a desktop kept shipping with content running
off its right edge (the Settings landing was the latest, FoodAssistant-t9lj;
the Glance grid before it, FoodAssistant-f6ea). This sweep is the standing
guard: it walks the primary pages at that exact geometry and fails when any
page lays out wider than the viewport, so the clip class of bug cannot ship
silently again.

Run at the default UI scale on purpose: the kiosk zoom rescales the CSS canvas
(making scrollWidth legitimately exceed innerWidth), which would mask real
overflow. At scale 1.0 the invariant is exact: scrollWidth == viewport width.
"""
from __future__ import annotations

import pytest

# The pages a kitchen actually lives on, plus the standalone (non-base.html)
# pages that historically missed the kiosk safe-area handling.
PAGES = [
    "/ui/start",
    "/ui/inventory",
    "/ui/expiring",
    "/ui/add",
    "/ui/pending",
    "/ui/cook",
    "/ui/recipes",
    "/ui/shopping",
    "/ui/kitchen-guide",
    "/ui/timers",
    "/ui/weather",
    "/setup",
]

PORTRAIT = (480, 800)
# A couple of px of slack for scrollbar/rounding differences across engines.
TOLERANCE = 2


@pytest.mark.parametrize("path", PAGES)
def test_page_has_no_horizontal_overflow_portrait(new_page, path):
    page = new_page(path, kiosk=True, viewport=PORTRAIT)
    # Let late pollers/first paints settle; overflow from async content (cards,
    # tiles) is exactly what this sweep exists to catch.
    page.wait_for_timeout(1500)
    metrics = page.evaluate(
        "() => ({scroll: document.documentElement.scrollWidth,"
        "        inner: window.innerWidth})"
    )
    assert metrics["scroll"] <= metrics["inner"] + TOLERANCE, (
        f"{path} lays out {metrics['scroll']}px wide in a "
        f"{metrics['inner']}px portrait viewport: content is clipped on the "
        f"kiosk panel (see FoodAssistant-t9lj)"
    )


# The other half of the clip class: some panels make Chromium lay out WIDER
# than the visible screen (the Bandit reports innerWidth ~20px past
# screen.width). kiosk-display.js turns that gap into --kiosk-inset-* vars;
# every page must APPLY them or its right edge is cut off on the panel. The
# Settings page shipped without that application for months (t9lj), because
# nothing asserted it. These pages cover both plumbing paths: base.html pages
# get the padding from kiosk.css, the standalone pages carry their own rule.
# Each page applies the inset to its outermost padded surface: base.html pages
# and Settings pad the body, the Start page pads its full-screen wrapper.
INSET_PAGES = [("/setup", "body"), ("/ui/start", ".start-wrap"),
               ("/ui/inventory", "body")]


@pytest.mark.parametrize("path,surface", INSET_PAGES)
def test_panel_overscan_inset_is_applied(browser, app_server, path, surface):
    ctx = browser.new_context(viewport={"width": 480, "height": 800})
    try:
        ctx.add_init_script(
            "try { localStorage.setItem('kioskMode', 'true'); } catch (e) {}"
            # Simulate the panel quirk: the visible screen is 20px narrower
            # than the CSS viewport Chromium lays out into.
            "Object.defineProperty(screen, 'width', {value: 460});"
        )
        page = ctx.new_page()
        page.goto(app_server + path, wait_until="networkidle")
        page.wait_for_timeout(800)
        pad = page.evaluate(
            "(sel) => parseFloat(getComputedStyle("
            "document.querySelector(sel)).paddingRight)", surface
        )
        assert pad >= 18, (
            f"{path} ignores the panel safe-area inset (body padding-right "
            f"{pad}px, expected ~20px): its right edge is clipped on panels "
            f"like the Bandit's (FoodAssistant-t9lj)"
        )
    finally:
        ctx.close()
