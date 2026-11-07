"""The Grocy location items on the Pending page (services/grocy_locations.py).

Each item carries its own button for someone who may change settings, the
merge asks first, and when Grocy refuses, the reason stays on the item, also
through the inbox's own refresh. At phone width nothing runs off the side.
"""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.browser

ITEMS = [
    {"id": 901, "kind": "grocy_two_fridges", "level": "info", "status": "open",
     "title": "Your Grocy has two fridges, Fridge and Refrigerator. Merge them?",
     "body": "Merging moves everything in Fridge to Refrigerator.",
     "payload": {"from_id": 2, "from_name": "Fridge", "to_id": 3, "to_name": "Refrigerator"}},
    {"id": 902, "kind": "grocy_freezer_flag", "level": "info", "status": "open",
     "title": "Mark Freezer as a freezer in Grocy?",
     "body": "Grocy does not know Freezer is a freezer.",
     "payload": {"id": 4, "name": "Freezer"}},
]


def _inbox(page, items):
    def answer(route):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"items": items, "count": len(items)}))
    page.route("**/action-items", answer)


def test_each_item_has_its_button_and_a_refusal_stays_on_it(new_page):
    page = new_page("/ui/pending", viewport=(390, 844))
    _inbox(page, ITEMS)
    posted = []

    def refuse(route):
        posted.append(route.request.url)
        route.fulfill(status=409, content_type="application/json", body=json.dumps(
            {"ok": False, "error": "Fridge was not hidden. Still in Fridge: Flour."}))

    page.route("**/setup/grocy/merge-fridges", refuse)
    page.reload(wait_until="load")
    card = page.locator("#action-items-list .card", has_text="two fridges")
    card.get_by_role("button", name="Merge", exact=True).wait_for()
    flag = page.locator("#action-items-list .card", has_text="Mark Freezer")
    assert flag.get_by_role("button", name="Mark as freezer").is_visible()

    card.get_by_role("button", name="Merge", exact=True).click()
    dialog = page.locator(".modal.show")
    dialog.wait_for()
    assert "Merge Fridge into Refrigerator?" in dialog.inner_text()
    dialog.locator(".btn", has_text="Merge").last.click()
    error = card.locator(".grocy-fix-error")
    error.wait_for()
    assert posted and error.inner_text() == "Fridge was not hidden. Still in Fridge: Flour."
    # The inbox redraws itself; the reason is still there afterwards.
    page.evaluate("loadActionItems()")
    page.wait_for_timeout(300)
    card = page.locator("#action-items-list .card", has_text="two fridges")
    assert card.locator(".grocy-fix-error").inner_text().startswith("Fridge was not hidden")
    width = page.evaluate("document.documentElement.scrollWidth")
    assert width <= 390


def test_a_done_fix_leaves_the_inbox(new_page):
    page = new_page("/ui/pending", viewport=(390, 844))
    items = list(ITEMS)
    _inbox(page, items)

    def done(route):
        items[:] = [i for i in items if i["kind"] != "grocy_freezer_flag"]
        route.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"ok": True, "message": "Freezer is now a freezer in Grocy."}))

    page.route("**/setup/grocy/mark-freezer", done)
    page.reload(wait_until="load")
    page.get_by_role("button", name="Mark as freezer").click()
    page.locator("#pToastMsg", has_text="Freezer is now a freezer in Grocy.").wait_for()
    page.wait_for_function(
        "!document.querySelector('#action-items-list').textContent.includes('Mark Freezer')")
