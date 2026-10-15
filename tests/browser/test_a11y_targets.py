"""Touch targets, landmarks and headings on an 800x480 kitchen screen.

The top bar's links came out at 43px and the Settings menu entries at 40px,
just under the 44px a fingertip lands on reliably. Review, Kitchen Guide and
Settings had no h1, and the no-password banner took about 60px of the panel.
"""
import pytest

_HEIGHTS = ("els => els.filter(e => e.offsetParent)"
            ".map(e => [e.textContent.trim().slice(0, 20), e.getBoundingClientRect().height])")


def _short(items):
    return [(label, h) for label, h in items if h < 44 - 0.5]


def test_kiosk_top_bar_and_menus_are_44px(new_page):
    page = new_page("/ui/inventory", kiosk=True, viewport=(800, 480))
    top = page.eval_on_selector_all(".navbar .nav-primary .nav-link", _HEIGHTS)
    assert top and not _short(top), top

    page.click(".navbar-toggler")
    page.wait_for_selector("#nav.show")
    menu = page.eval_on_selector_all("#nav .nav-link", _HEIGHTS)
    assert menu and not _short(menu), menu

    page.click("#moreMenu")
    page.wait_for_selector(".dropdown-menu.show")
    more = page.eval_on_selector_all(".dropdown-menu.show .dropdown-item", _HEIGHTS)
    assert more and not _short(more), more


def test_kiosk_top_bar_still_clears_the_page(new_page):
    # Taller links must not push the fixed bar down over the sub-page pills.
    page = new_page("/ui/inventory", kiosk=True, viewport=(800, 480))
    bar = page.eval_on_selector(".navbar.fixed-top", "e => e.getBoundingClientRect().bottom")
    pills = page.eval_on_selector("#subPillBar", "e => e.getBoundingClientRect().top")
    assert bar <= pills + 0.5, (bar, pills)


def test_kiosk_settings_menu_is_44px(new_page):
    page = new_page("/setup", kiosk=True, viewport=(800, 480))
    items = page.eval_on_selector_all(".side-menu .nav-link", _HEIGHTS)
    assert items and not _short(items), items


def test_access_banner_is_one_compact_row_on_a_kiosk(new_page):
    page = new_page("/ui/inventory", kiosk=True, viewport=(800, 480))
    page.wait_for_selector("#pr-open-notice")
    height = page.eval_on_selector("#pr-access-banner", "e => e.getBoundingClientRect().height")
    assert height <= 46, height
    # The link to set a password is never the part that gets trimmed.
    right = page.eval_on_selector("#pr-open-notice .pr-access-link", "e => e.getBoundingClientRect().right")
    assert right <= 800, right


@pytest.mark.parametrize("path,title", [
    ("/ui/pending", "Review"),
    ("/ui/kitchen-guide", "Kitchen Guide"),
    ("/setup", "Settings"),
    ("/ui/add", "Manage Pantry"),
    ("/ui/start", "Start"),
])
def test_page_has_one_h1(new_page, path, title):
    page = new_page(path)
    h1s = page.eval_on_selector_all("h1", "els => els.map(e => e.textContent.replace(/\\s+/g, ' ').trim())")
    assert len(h1s) == 1 and title in h1s[0], h1s
    assert page.eval_on_selector_all("main", "els => els.length") == 1


def test_manage_headings_step_down_one_level_at_a_time(new_page):
    page = new_page("/ui/add")
    levels = page.eval_on_selector_all(
        "main h1, main h2, main h3, main h4, main h5, main h6",
        "els => els.map(e => Number(e.tagName[1]))")
    assert levels and levels[0] == 1, levels
    for prev, cur in zip(levels, levels[1:]):
        assert cur <= prev + 1, levels


def test_floating_chrome_sits_in_landmarks(new_page):
    page = new_page("/ui/inventory", kiosk=True, viewport=(800, 480))
    assert page.eval_on_selector("#subPillBar", "e => e.closest('nav, main, aside') !== null")
    assert page.eval_on_selector("#timerChips", "e => e.tagName") == "ASIDE"
