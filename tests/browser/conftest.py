"""Shared harness for the headless-browser suite (FoodAssistant-yc0m).

Boots the app once per session against the mock Grocy/Mealie backend from
tests/browser/_mockstack.py (shared with scripts/capture-screenshots.py) and
drives it with Playwright Chromium. Every test in this directory is marked
``browser`` and the whole directory auto-skips when Playwright or a Chromium
binary is unavailable, so the pure-logic suite stays runnable anywhere.

Chromium resolution order: CHROMIUM_EXECUTABLE, then a ``chromium`` entry
under PLAYWRIGHT_BROWSERS_PATH, then Playwright's own installed build.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import _mockstack  # noqa: E402


def _availability() -> tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False, "playwright is not installed"
    kwargs = {}
    exe = _mockstack.chromium_executable()
    if exe:
        kwargs["executable_path"] = exe
    # A real launch probe, not just a file check: the binary can exist while
    # its system libraries are missing, and that must skip cleanly too.
    try:
        with sync_playwright() as pw:
            pw.chromium.launch(**kwargs).close()
        return True, ""
    except Exception as e:  # noqa: BLE001 - any launch failure means no browser
        return False, f"chromium failed to launch: {str(e).splitlines()[0]}"


BROWSER_AVAILABLE, SKIP_REASON = _availability()

_HERE = Path(__file__).parent


def pytest_collection_modifyitems(config, items):
    """Mark everything under tests/browser/ and skip it all without a browser."""
    skip = pytest.mark.skip(reason=f"browser tests skipped: {SKIP_REASON}")
    for item in items:
        try:
            in_dir = Path(str(item.fspath)).is_relative_to(_HERE)
        except (ValueError, OSError):
            in_dir = False
        if not in_dir:
            continue
        item.add_marker(pytest.mark.browser)
        if not BROWSER_AVAILABLE:
            item.add_marker(skip)


# App boot settings beyond the shared demo defaults: the Start Page on with
# timer keys (for the live-face test), and the idle screensaver never
# self-starting (tests force it through window.__screensaverTest).
_EXTRA_ENV = {
    "START_PAGE_ENABLED": "true",
    "START_PAGE_KEYS": "6",
    "START_PAGE_LAYOUT": '["timer_1", "timer_eggs", "inventory", "add", "blank", "blank"]',
    "SCREENSAVER_MINUTES": "0",
}


@pytest.fixture(scope="session")
def app_server():
    """The booted app's base URL (uvicorn subprocess + mock Grocy/Mealie)."""
    with _mockstack.boot_app(_EXTRA_ENV) as base:
        yield base


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright
    kwargs = {}
    exe = _mockstack.chromium_executable()
    if exe:
        kwargs["executable_path"] = exe
    with sync_playwright() as pw:
        b = pw.chromium.launch(**kwargs)
        yield b
        b.close()


@pytest.fixture
def new_page(browser, app_server):
    """Factory for a page on the booted app.

    ``new_page(path, kiosk=..., viewport=(w, h), zoom=...)``:
      - ``kiosk=True`` latches kiosk mode (localStorage kioskMode) before any
        page script runs, the same gate the real appliance display sets.
      - ``viewport`` sets the context's window size (rotation = swap w/h).
      - ``zoom`` applies a kiosk interface scale as html zoom, mirroring what
        kiosk-display.js does for the per-device scale setting.
    Contexts are closed at test end.
    """
    contexts = []

    def make(path="/ui/inventory", *, kiosk=False, viewport=(1280, 800), zoom=None):
        ctx = browser.new_context(
            viewport={"width": viewport[0], "height": viewport[1]})
        contexts.append(ctx)
        if kiosk:
            ctx.add_init_script(
                "try { localStorage.setItem('kioskMode', 'true'); } catch (e) {}")
        page = ctx.new_page()
        page.goto(app_server + path, wait_until="networkidle")
        if zoom:
            set_zoom(page, zoom)
        return page

    yield make
    for ctx in contexts:
        ctx.close()


def set_zoom(page, zoom: float) -> None:
    """Apply the kiosk interface scale the way kiosk-display.js does."""
    page.evaluate(f"document.documentElement.style.zoom = '{zoom}'")


@pytest.fixture(name="set_zoom")
def set_zoom_fixture():
    return set_zoom
