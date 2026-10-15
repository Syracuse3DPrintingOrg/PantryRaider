"""Number boxes on the on-screen keyboard, and the page's own dialogs.

Taps are real touch events (a touch-enabled context and locator.tap()), since
a click proves nothing about the keyboard keeping the focus in the box. The
dialog checks run the converted calls on their real pages: Clear all on the
Timers page, Mark consumed on the Expiring page (a question before a form
submit), and the kiosk mode toggle in the navigation bar.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


@pytest.fixture
def touch_page(browser, app_server):
    contexts = []

    def make(path, *, kiosk=True):
        ctx = browser.new_context(viewport={"width": 1280, "height": 800}, has_touch=True)
        contexts.append(ctx)
        if kiosk:
            ctx.add_init_script("try { localStorage.setItem('kioskMode', 'true'); } catch (e) {}")
        page = ctx.new_page()
        page.errors = []
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        page.goto(app_server + path, wait_until="load")
        return page

    yield make
    for ctx in contexts:
        ctx.close()


def _key(page, ch):
    page.locator(f'#pr-osk [data-osk-char="{ch}"]').tap()


def _action(page, name):
    page.locator(f'#pr-osk [data-osk-action="{name}"]').tap()


def _watch_changes(page, selector):
    page.evaluate("""sel => {
        window.__changes = [];
        document.querySelector(sel).addEventListener('change',
            e => window.__changes.push(e.target.value));
    }""", selector)


def test_number_box_takes_a_decimal_and_fires_change_on_leaving(touch_page):
    page = touch_page("/ui/timers")
    _watch_changes(page, "#timerCustomMinutes")
    page.locator("#timerCustomMinutes").tap()
    page.wait_for_selector("#pr-osk.pr-osk-number.pr-osk-open", timeout=5000)

    _key(page, "1")
    _key(page, ".")
    # "1." is not a number yet; the box shows what it can and keeps the rest.
    assert page.input_value("#timerCustomMinutes") == "1"
    _key(page, "5")
    assert page.input_value("#timerCustomMinutes") == "1.5"
    # One decimal point only, and no minus sign: this box's min is 1.
    _key(page, ".")
    _key(page, "-")
    assert page.input_value("#timerCustomMinutes") == "1.5"
    assert page.evaluate("document.activeElement.id") == "timerCustomMinutes"
    assert page.evaluate("window.__changes") == []

    page.locator("#timerCustomLabel").tap()   # leave the box
    assert page.evaluate("window.__changes") == ["1.5"]
    assert page.errors == []


def test_number_box_takes_a_minus_sign_and_fires_change_on_enter(touch_page):
    page = touch_page("/ui/timers")
    # A box whose min allows a negative number (a freezer threshold, say).
    page.evaluate("""() => {
        const box = document.createElement('input');
        box.type = 'number';
        box.id = 'negBox';
        box.className = 'form-control';
        document.querySelector('main').prepend(box);
    }""")
    _watch_changes(page, "#negBox")
    page.locator("#negBox").tap()
    page.wait_for_selector("#pr-osk.pr-osk-number.pr-osk-open", timeout=5000)
    _key(page, "-")
    assert page.input_value("#negBox") == ""
    _key(page, "2")
    assert page.input_value("#negBox") == "-2"
    _action(page, "ENTER")
    assert page.evaluate("window.__changes") == ["-2"]
    # Backspace works on the typed text, and change fires again on leaving.
    _action(page, "BKSP")
    _key(page, "7")
    assert page.input_value("#negBox") == "-7"
    _action(page, "DONE")
    assert page.evaluate("window.__changes") == ["-2", "-7"]
    assert page.errors == []


def test_ask_dialog_is_typed_into_with_the_on_screen_keyboard(touch_page):
    page = touch_page("/ui/timers")
    page.locator("#timerCustomLabel").tap()        # the opener, gets focus back
    page.evaluate("""() => {
        window.__ans = 'pending';
        PR.ask('For example Grill or Smoker.',
               { title: 'Name this thermometer', default: 'Old', ok: 'Save' })
          .then(v => { window.__ans = v; });
    }""")
    dialog = page.locator(".pr-dialog-wrap [role=dialog]")
    dialog.wait_for(timeout=5000)
    labelled = dialog.get_attribute("aria-labelledby")
    assert page.locator(f"#{labelled}").inner_text() == "Name this thermometer"
    assert page.evaluate("document.activeElement.closest('.pr-dialog-wrap') !== null")
    assert page.input_value(".pr-dialog-wrap input") == "Old"
    page.wait_for_selector("#pr-osk.pr-osk-open", timeout=5000)

    # The dialog's layer is shortened so its buttons sit above the keys.
    kb_top = page.evaluate("document.getElementById('pr-osk').getBoundingClientRect().top")
    footer_bottom = page.evaluate(
        "document.querySelector('.pr-dialog-wrap .modal-footer').getBoundingClientRect().bottom")
    assert footer_bottom <= kb_top + 1

    # The default text is selected, so the first key replaces it.
    for ch in "grill":
        _key(page, ch)
    assert page.input_value(".pr-dialog-wrap input") == "grill"
    _action(page, "ENTER")
    page.wait_for_function("window.__ans !== 'pending'")
    assert page.evaluate("window.__ans") == "grill"
    assert page.evaluate("document.activeElement.id") == "timerCustomLabel"
    page.wait_for_selector(".pr-dialog-wrap", state="detached", timeout=3000)

    # Escape is Cancel, which answers null.
    page.evaluate("""() => { window.__ans = 'pending';
        PR.ask('Name?').then(v => { window.__ans = v; }); }""")
    page.locator(".pr-dialog-wrap input").wait_for()
    page.keyboard.press("Escape")
    page.wait_for_function("window.__ans !== 'pending'")
    assert page.evaluate("window.__ans") is None
    assert page.errors == []


def test_timers_clear_all_asks_in_the_page(touch_page, app_server):
    page = touch_page("/ui/timers", kiosk=False)
    page.request.delete(app_server + "/timers")
    page.request.post(app_server + "/timers", data={"label": "eggs", "seconds": 600})
    page.reload(wait_until="load")
    page.wait_for_selector("#timersClearAll:not(.d-none)", timeout=5000)

    def count():
        return len(page.request.get(app_server + "/timers").json().get("timers", []))

    page.locator("#timersClearAll").click()
    dialog = page.locator(".pr-dialog-wrap [role=dialog]")
    dialog.wait_for(timeout=5000)
    assert "Stop and clear the timer?" in dialog.inner_text()
    dialog.get_by_role("button", name="Cancel").click()
    page.wait_for_timeout(300)
    assert count() == 1

    page.locator("#timersClearAll").click()
    page.locator(".pr-dialog-wrap [role=dialog]").get_by_role("button", name="Clear").click()
    for _ in range(20):
        if count() == 0:
            break
        page.wait_for_timeout(150)
    assert count() == 0
    assert page.errors == []


def test_expiring_consume_asks_before_the_form_submits(touch_page):
    page = touch_page("/ui/expiring?days=30", kiosk=False)
    form = page.locator('form[action^="ui/consume/"]').first
    form.wait_for(timeout=5000)
    url_before = page.url
    form.locator("button[type=submit]").click()
    dialog = page.locator(".pr-dialog-wrap [role=dialog]")
    dialog.wait_for(timeout=5000)
    assert "as consumed?" in dialog.inner_text()
    page.keyboard.press("Escape")        # Cancel: the form stays put
    page.wait_for_timeout(400)
    assert page.url == url_before

    form.locator("button[type=submit]").click()
    with page.expect_navigation(timeout=10000):
        page.locator(".pr-dialog-wrap [role=dialog]").get_by_role(
            "button", name="Mark consumed").click()
    assert page.errors == []


def test_kiosk_toggle_asks_in_the_page(touch_page):
    page = touch_page("/ui/inventory", kiosk=False)
    page.locator("#kioskToggle").click()
    dialog = page.locator(".pr-dialog-wrap [role=dialog]")
    dialog.wait_for(timeout=5000)
    assert "Turn kiosk mode on now?" in dialog.inner_text()
    dialog.get_by_role("button", name="Cancel").click()
    assert page.evaluate("localStorage.getItem('kioskMode')") != "true"

    page.locator("#kioskToggle").click()
    with page.expect_navigation(timeout=10000):
        page.keyboard.press("Enter")     # Enter answers OK
    assert page.evaluate("localStorage.getItem('kioskMode')") == "true"
    assert page.errors == []


@pytest.mark.parametrize("path", [
    "/ui/timers", "/ui/expiring", "/ui/pending", "/ui/cook", "/ui/recipes",
    "/ui/current-recipe", "/ui/audit", "/ui/add", "/ui/defaults", "/ui/inventory",
    "/setup",
])
def test_pages_with_converted_dialogs_load_without_script_errors(touch_page, path):
    page = touch_page(path, kiosk=False)
    page.wait_for_timeout(500)
    assert page.evaluate("typeof PR.ask === 'function' && typeof PR.confirmBox === 'function'")
    assert page.errors == []


def test_dialog_over_an_open_bootstrap_modal_keeps_the_focus(touch_page):
    # Bootstrap pulls the focus back into its own modal from anywhere else, so
    # a question asked from inside one is drawn inside it.
    page = touch_page("/ui/recipes", kiosk=False)
    page.evaluate("bootstrap.Modal.getOrCreateInstance("
                  "document.getElementById('importUrlModal')).show()")
    page.wait_for_selector("#importUrlModal.show", timeout=5000)
    page.wait_for_timeout(400)
    page.evaluate("""() => { window.__ans = 'pending';
        PR.ask('Name?', { default: '' }).then(v => { window.__ans = v; }); }""")
    page.locator(".pr-dialog-wrap input").wait_for(timeout=5000)
    assert page.evaluate("!!document.querySelector('#importUrlModal .pr-dialog-wrap')")
    page.keyboard.type("abc")
    for _ in range(3):
        page.keyboard.press("Tab")
    assert page.evaluate("!!document.activeElement.closest('.pr-dialog-wrap')")
    assert page.input_value(".pr-dialog-wrap input") == "abc"
    # Escape answers the dialog and leaves the modal underneath open.
    page.keyboard.press("Escape")
    page.wait_for_function("window.__ans !== 'pending'")
    assert page.evaluate("window.__ans") is None
    page.wait_for_timeout(500)
    assert page.evaluate("document.getElementById('importUrlModal').classList.contains('show')")
    assert page.errors == []
