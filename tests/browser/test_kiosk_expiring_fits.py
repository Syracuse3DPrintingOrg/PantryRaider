"""The Expiring page fits an 800x480 kitchen screen.

Kiosk mode stacks page columns one per line; the four summary counts opt out
(they pushed the item list off the panel), and each item becomes one block so
its buttons never run off the right edge (seen on a Pi 4 kiosk, 2026-10-15).
"""


def test_expiring_fits_a_kitchen_screen(new_page):
    page = new_page("/ui/expiring?days=30", kiosk=True, viewport=(800, 480))
    tops = page.eval_on_selector_all(
        "#expiring-summary > [class*='col-']", "els => els.map(e => Math.round(e.getBoundingClientRect().top))")
    assert len(tops) == 4 and len(set(tops)) == 1, tops   # one row of four
    overflow = page.evaluate(
        "() => [...document.querySelectorAll('#expiring-table button, #expiring-table a')]"
        ".filter(b => b.offsetParent).map(b => b.getBoundingClientRect().right)"
        ".filter(r => r > innerWidth + 1).length")
    assert overflow == 0
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")


def test_kiosk_checkboxes_stay_square(new_page):
    # Kiosk mode gives text fields a 44px minimum height; a checkbox caught by
    # that rule turned into a tall slab on the Recipes filter.
    page = new_page("/ui/recipes", kiosk=True, viewport=(800, 480))
    sizes = page.eval_on_selector_all(
        "input.form-check-input[type=checkbox]",
        "els => els.filter(e => e.offsetParent).map(e => [e.offsetWidth, e.offsetHeight])")
    assert sizes
    for w, h in sizes:
        assert abs(w - h) <= 2 and h < 40, sizes


def test_nutrition_totals_sit_in_one_row_on_a_kiosk(new_page):
    page = new_page("/ui/nutrition", kiosk=True, viewport=(800, 480))
    tops = page.eval_on_selector_all(
        "#nt-totals > [class*='col-']", "els => els.map(e => Math.round(e.getBoundingClientRect().top))")
    assert len(tops) == 4 and len(set(tops)) == 1, tops
