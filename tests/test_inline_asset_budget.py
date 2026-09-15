"""Inline CSS and JS in a template is re-parsed by the browser on every
navigation, because it cannot be cached the way a static file can.

Measured on the Bandit (Pi 4, Chromium 150, 2026-09-15): on a warm kiosk the
server answered in about 40 ms and a page still took 300 to 500 ms to load,
almost all of it the browser rebuilding the document. base.html was carrying
18 KB of inline <style> and 13 KB of inline <script> on every page, and
timers.html 29 KB of inline script, none of it needing Jinja. That is now in
static files served with a one-year immutable cache. This file keeps it there.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "service" / "app" / "templates"
STATIC = ROOT / "service" / "app" / "static"

_STYLE = re.compile(r"<style\b[^>]*>(.*?)</style>", re.S)
_INLINE_SCRIPT = re.compile(r"<script\b(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


def _inline_bytes(name: str) -> tuple[int, int]:
    src = (TEMPLATES / name).read_text()
    return (sum(len(m) for m in _STYLE.findall(src)),
            sum(len(m) for m in _INLINE_SCRIPT.findall(src)))


def test_base_keeps_only_the_dynamic_rules_inline():
    """What stays inline is what genuinely depends on per-install settings:
    custom theme colours, the background image and its opacity, the PWA
    registration, the before-first-paint kiosk CSS hook, and one small IIFE
    that sits before the content block. Everything else lives in
    static/css/base.css and static/js/base-page.js."""
    style, script = _inline_bytes("base.html")
    assert style < 3000, f"base.html inline <style> grew back to {style} B"
    assert script < 3500, f"base.html inline <script> grew back to {script} B"


def test_timers_page_has_no_inline_assets():
    style, script = _inline_bytes("timers.html")
    assert style == 0 and script == 0, (style, script)


def test_barcode_capture_stays_behind_its_condition():
    """Global barcode capture used to be an inline block emitted only when the
    barcode_global_capture setting is on and the page is not Add. Moving it to
    a file must not make it load everywhere: the same {% if %} has to wrap the
    <script src> tag, or every page starts swallowing keystrokes."""
    base = (TEMPLATES / "base.html").read_text()
    i = base.index("barcode-capture.js")
    opening = base.rfind("{% if barcode_global_capture and active != 'add' %}", 0, i)
    assert opening != -1, "the barcode script tag is no longer inside its conditional"
    closing = base.find("{% endif %}", i)
    assert closing != -1 and "{% if" not in base[opening + 10:i], "conditional does not wrap the tag cleanly"
    # And it must be the only thing between the condition and the endif.
    assert base[opening:closing].count("<script") == 1


def test_extracted_files_exist_and_carry_no_template_tags():
    for rel in ("css/base.css", "js/base-page.js", "js/barcode-capture.js", "css/timers.css", "js/timers-page.js"):
        p = STATIC / rel
        assert p.exists(), rel
        text = p.read_text()
        assert "{{" not in text and "{%" not in text, f"{rel} contains Jinja"


def test_extracted_files_are_referenced_with_cache_busting():
    base = (TEMPLATES / "base.html").read_text()
    timers = (TEMPLATES / "timers.html").read_text()
    assert 'href="static/css/base.css?v={{ app_version }}"' in base
    assert 'src="static/js/base-page.js?v={{ app_version }}"' in base
    assert 'href="static/css/timers.css?v={{ app_version }}"' in timers
    assert 'src="static/js/timers-page.js?v={{ app_version }}"' in timers


def test_base_page_script_is_synchronous_after_bootstrap():
    """The seven blocks it replaced ran synchronously right after bootstrap
    and before {% block scripts %}. Deferring it would replay the 0.18.97
    regression, where a page script ran before the globals it read existed."""
    base = (TEMPLATES / "base.html").read_text()
    tag = re.search(r'<script[^>]*base-page\.js[^>]*>', base).group(0)
    assert "defer" not in tag and "async" not in tag
    assert base.index("bootstrap.bundle.min.js") < base.index("base-page.js") < base.index("{% block scripts %}")


def test_theme_branch_became_attribute_scoped_rules():
    css = (STATIC / "css" / "base.css").read_text()
    assert '[data-bs-theme="light"] .btn-outline-info' in css
    assert '[data-bs-theme="dark"] .btn-outline-info' in css


def test_kiosk_toggle_is_still_reachable_from_markup():
    """base.html's onclick calls toggleKioskMode(); the move must keep it a
    global, not tuck it into a closure."""
    js = (STATIC / "js" / "base-page.js").read_text()
    assert re.search(r"^function toggleKioskMode\(", js, re.M), "toggleKioskMode is no longer a top-level function"
    assert 'onclick="toggleKioskMode(); return false;"' in (TEMPLATES / "base.html").read_text()


def test_timers_initial_view_rides_on_the_page_root():
    timers = (TEMPLATES / "timers.html").read_text()
    js = (STATIC / "js" / "timers-page.js").read_text()
    assert 'data-initial-view="{{ initial_view or \'\' }}"' in timers
    assert "dataset || {}).initialView" in js
