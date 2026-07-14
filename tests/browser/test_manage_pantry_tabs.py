"""Manage Pantry mode tabs ARE the shared scanner mode (FoodAssistant-7ss1).

Clicking a tab must POST pending/scanner-mode so the USB scanner and the
Stream Deck key follow, and the page must reflect a mode changed elsewhere.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


def _server_mode(page, app_server) -> str:
    return page.request.get(app_server + "/pending/scanner-mode").json().get("mode")


def _wait_for_mode(page, app_server, mode: str, tries: int = 20) -> str:
    got = _server_mode(page, app_server)
    while got != mode and tries:
        page.wait_for_timeout(250)
        tries -= 1
        got = _server_mode(page, app_server)
    return got


def test_mode_tabs_post_shared_scanner_mode(new_page, app_server):
    page = new_page("/ui/add")
    try:
        for mode in ("consume", "shopping", "audit", "inventory"):
            page.click(f"#mode-tab-{mode}")
            # The tab click POSTs the shared mode; the server must agree.
            assert _wait_for_mode(page, app_server, mode) == mode
            # And the page shows that tab as the active pane.
            assert "active" in (page.get_attribute(f"#mode-tab-{mode}", "class") or "")
            assert not page.locator(f"#mode-pane-{mode}").is_hidden()
    finally:
        page.request.post(app_server + "/pending/scanner-mode",
                          data={"mode": "inventory"})


def test_page_follows_mode_changed_elsewhere(new_page, app_server):
    page = new_page("/ui/add")
    try:
        # Change the mode behind the page's back (a Stream Deck key press).
        page.request.post(app_server + "/pending/scanner-mode",
                          data={"mode": "consume"})
        # The page polls the shared mode and moves its tab to match.
        page.wait_for_selector("#mode-tab-consume.active", timeout=10000)
    finally:
        page.request.post(app_server + "/pending/scanner-mode",
                          data={"mode": "inventory"})
