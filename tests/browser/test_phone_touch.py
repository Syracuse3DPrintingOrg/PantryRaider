"""The app on a phone: touch-sized controls, nothing off the right edge, and
the everyday journeys (inventory, expiring, shopping) driven with taps.

A 390x844 touch context (has_touch + is_mobile), so the page sees
pointer: coarse the way a real phone browser does. The inventory checks also
run from the keyboard on a desktop window: the category headers are buttons.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

PHONE = {"viewport": {"width": 390, "height": 844}, "has_touch": True, "is_mobile": True}

_MEASURE = """sel => [...document.querySelectorAll(sel)]
  .filter(e => e.offsetParent !== null)
  .map(e => { const r = e.getBoundingClientRect();
              return {w: r.width, h: r.height, right: r.right}; })"""


@pytest.fixture
def phone(browser, app_server):
    contexts = []

    def make(path):
        ctx = browser.new_context(**PHONE)
        contexts.append(ctx)
        page = ctx.new_page()
        page.errors = []
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        page.goto(app_server + path, wait_until="load")
        return page

    yield make
    for ctx in contexts:
        ctx.close()


def _no_sideways_scroll(page):
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth")


def test_inventory_row_buttons_are_finger_sized_and_on_screen(phone):
    page = phone("/ui/inventory")
    page.wait_for_selector(".item-row .btn-icon")
    sizes = page.evaluate(_MEASURE, ".item-row .btn-icon")
    assert sizes
    for s in sizes:
        assert s["w"] >= 43.5 and s["h"] >= 43.5, s
        assert s["right"] <= 390, s
    _no_sideways_scroll(page)
    # Each button says which item it acts on.
    label = page.locator(".item-row .btn-icon").first.get_attribute("aria-label")
    assert label and label.startswith("Edit ")
    assert not page.errors


def test_inventory_consume_asks_before_emptying_the_stock(phone):
    page = phone("/ui/inventory")
    page.wait_for_selector(".item-row")
    row = page.locator(".item-row").first
    row_id = row.get_attribute("id")
    row.locator("[aria-label^='Mark '][aria-label$=' consumed']").tap()
    dialog = page.locator(".pr-dialog-wrap .modal")
    dialog.wait_for()
    assert "removes it from your stock" in dialog.inner_text()
    dialog.get_by_role("button", name="Cancel").tap()
    page.wait_for_selector(".pr-dialog-wrap", state="detached", timeout=3000)
    assert page.locator(f"#{row_id}").count() == 1


def test_inventory_category_headers_work_from_the_keyboard(new_page):
    page = new_page("/ui/inventory")
    header = page.locator("[data-toggle-panel]").first
    header.wait_for()
    assert header.get_attribute("aria-expanded") == "true"
    header.focus()
    page.keyboard.press("Enter")
    assert header.get_attribute("aria-expanded") == "false"
    page.keyboard.press(" ")
    assert header.get_attribute("aria-expanded") == "true"


def test_expiring_rows_fit_a_phone(phone):
    page = phone("/ui/expiring")
    page.wait_for_selector("#expiring-table tbody tr")
    _no_sideways_scroll(page)
    for s in page.evaluate(_MEASURE, "#expiring-table .exp-actions button"):
        assert s["right"] <= 390, s
        assert s["h"] >= 43.5, s


def test_shopping_add_check_and_clear(phone):
    page = phone("/ui/shopping")
    page.locator("#new-item").fill("Phone test capers")
    page.keyboard.press("Enter")
    item = page.locator("#items label", has_text="Phone test capers")
    item.wait_for(timeout=5000)
    remove = page.locator("[aria-label='Remove Phone test capers from the list']")
    assert remove.count() == 1
    box = remove.bounding_box()
    assert box["width"] >= 43.5 and box["height"] >= 43.5
    # Tapping the words checks the box (the label is the target, not just
    # the small square).
    item.tap()
    page.wait_for_timeout(300)
    page.get_by_role("button", name="Clear checked").tap()
    item.wait_for(state="detached", timeout=5000)
    _no_sideways_scroll(page)
    assert not page.errors


def test_recipe_actions_wrap_on_a_phone(phone):
    page = phone("/ui/recipes")
    page.wait_for_timeout(500)
    _no_sideways_scroll(page)


def test_manual_add_points_at_a_missing_name(phone):
    page = phone("/ui/add")
    page.locator("button.nav-link", has_text="Manual").tap()
    page.locator("[data-on-click='addManualItem()']").tap()
    box = page.locator("#manual-name")
    assert box.get_attribute("aria-invalid") == "true"
    assert page.locator("#manual-name-error").is_visible()
    assert page.locator(".pr-dialog-wrap").count() == 0
    box.fill("Phone test pears")
    assert box.get_attribute("aria-invalid") is None
