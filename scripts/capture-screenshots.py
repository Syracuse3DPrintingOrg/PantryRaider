#!/usr/bin/env python3
"""Regenerate the README screenshots in docs/screenshots/.

Boots the app standalone against a built-in mock Grocy + Mealie (stdlib HTTP
server with believable demo data, no Docker and no network), then captures the
documented pages with headless Chromium via Playwright. The mock backend and
boot logic are shared with the headless-browser test suite: they live in
tests/browser/_mockstack.py.

Usage:
    pip install fastapi jinja2 itsdangerous pillow python-multipart \
        sqlalchemy pydantic-settings httpx uvicorn playwright
    python scripts/capture-screenshots.py [outdir]

Chromium comes from Playwright (`playwright install chromium`), or set
PLAYWRIGHT_BROWSERS_PATH to a prepared browser directory. Demo expiry dates are
generated relative to today so urgency badges always look right.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests" / "browser"))

from _mockstack import boot_app, chromium_executable  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "docs" / "screenshots"
VIEWPORT = {"width": 1400, "height": 900}


def main() -> int:
    with boot_app() as base:
        from playwright.sync_api import sync_playwright
        # page path, output file, selector that proves the data rendered
        shots = [
            ("/ui/inventory", "inventory.png", "text=Chicken Breast"),
            ("/ui/add", "add.png", None),
            ("/ui/cook", "cook.png", "text=Cheesy Chicken and Rice"),
            ("/ui/mealplan", "mealplan.png", "text=Shakshuka"),
            ("/setup#pane-scanning", "setup.png", None),
            ("/ui/expiring", "expiring.png", "text=Baby Spinach"),
        ]
        OUT.mkdir(parents=True, exist_ok=True)
        # A pinned browser build (e.g. CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium)
        # avoids re-downloading Chromium when the environment ships one.
        launch_kwargs = {}
        exe = chromium_executable()
        if exe:
            launch_kwargs["executable_path"] = exe
        with sync_playwright() as pw:
            browser = pw.chromium.launch(**launch_kwargs)
            page = browser.new_page(viewport=VIEWPORT)
            for path, fname, ready in shots:
                page.goto(base + path, wait_until="networkidle")
                if ready:
                    page.wait_for_selector(ready, timeout=15000)
                page.wait_for_timeout(700)  # let icon fonts and transitions settle
                page.evaluate("window.scrollTo(0, 0)")  # a #pane anchor scrolls the header away
                page.wait_for_timeout(150)
                page.screenshot(path=str(OUT / fname))
                print(f"captured {fname}")
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
