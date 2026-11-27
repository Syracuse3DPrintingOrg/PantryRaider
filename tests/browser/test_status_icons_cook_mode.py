"""Status icons never sit on cook mode's Exit (FoodAssistant-s243h).

status-icons.js pinned its row in the top right corner above everything on a
page with no bar of its own, and cook mode draws its own bar with Exit in that
corner: on a tablet's /screen/cook page the icons covered Exit and took the
tap, and over On the Line they hid under cook mode for the whole cook. While
cook mode is open the icons now sit in its bar, beside Exit, and go back to
the page's own bar when it closes. A tablet page's Exit is never pushed off
the screen by them either.

Sizes are the measured panels: an Echo Show 8 lays out 963 x 594 CSS px at
1.33125, a ThinkSmart View 492 x 788 at 1.625, and the Bandit's 800 x 480
panel runs at interface scale 0.85. The suite's app has no status icons
saved, so each page is handed its /status-icons answer by a route.
"""
from __future__ import annotations

import json

import httpx
import pytest

pytestmark = pytest.mark.browser

ECHO_SHOW = {"viewport": {"width": 963, "height": 594}, "device_scale_factor": 1.33125,
             "is_mobile": True, "has_touch": True}
THINKSMART = {"viewport": {"width": 492, "height": 788}, "device_scale_factor": 1.625,
              "is_mobile": True, "has_touch": True}
BANDIT = {"viewport": {"width": 800, "height": 480}}
DESKTOP = {"viewport": {"width": 1440, "height": 900}}

THREE = [{"id": "fridge", "name": "Fridge door", "icon": "fridge", "detail": "open"},
         {"id": "therm", "name": "Thermometer", "icon": "thermometer", "detail": "145°F"},
         {"id": "grill", "name": "Grill", "icon": "oven", "detail": "on"}]
TWELVE = [{"id": f"item{i}", "name": f"Item {i}", "icon": "generic", "detail": ""}
          for i in range(12)]

_KIOSK = ("try { localStorage.setItem('kioskMode', 'true');"
          " sessionStorage.setItem('kioskIntroShown', '1'); } catch (e) {}")
# The interface scale, as kiosk-display.js applies it on the Bandit.
_ZOOM = ("document.addEventListener('DOMContentLoaded', function () {"
         " document.documentElement.style.zoom = '0.85'; });")

# Where the row is, and whether Exit and every icon are on top where they
# are drawn (what a finger lands on), with no icon over Exit.
_LAYOUT = """() => {
  function box(el) { var r = el.getBoundingClientRect(); return {l: r.left, t: r.top, r: r.right, b: r.bottom}; }
  function onTop(el) {
    var r = el.getBoundingClientRect();
    var hit = document.elementFromPoint((r.left + r.right) / 2, (r.top + r.bottom) / 2);
    return !!hit && (hit === el || el.contains(hit));
  }
  var exit = null, all = document.querySelectorAll('.cm-exit, #sc-exit');
  for (var i = 0; i < all.length; i++) if (all[i].offsetWidth > 0) exit = all[i];
  var e = box(exit), row = document.querySelector('.si-row');
  var icons = [], list = document.querySelectorAll('.si-ic');
  for (var j = 0; j < list.length; j++) {
    var b = box(list[j]);
    icons.push({onTop: onTop(list[j]), inView: b.l >= 0 && b.r <= innerWidth,
                overExit: b.l < e.r && b.r > e.l && b.t < e.b && b.b > e.t});
  }
  var bar = row.parentNode;
  return {exit: exit.className || exit.id, exitOnTop: onTop(exit),
          exitInView: e.l >= 0 && e.t >= 0 && e.r <= innerWidth && e.b <= innerHeight,
          icons: icons, inCookBar: /\\bcm-top\\b/.test(bar.className),
          firstInBar: bar.firstElementChild === row,
          inNav: !!row.closest('nav.navbar'), cookOpen: !!document.querySelector('.cm-top') &&
            document.querySelector('.cm-top').offsetHeight > 0};
}"""


@pytest.fixture(scope="module")
def icons_on(app_server):
    """Status icons switched on for this module's pages, then off again."""
    def save(**fields):
        r = httpx.post(app_server + "/setup/save", json=fields, timeout=15)
        assert r.status_code == 200, r.text
    save(status_icons_enabled=True, status_icons_position="top_right")
    yield save
    save(status_icons_enabled=False, status_icons_position="top_right")


def _open(browser, base, path, device, items=THREE, *, kiosk=False, zoom=False):
    ctx = browser.new_context(**device)
    answer = json.dumps({"enabled": True, "position": "top_right", "items": items})
    ctx.route(lambda url: url.endswith("/status-icons"),
              lambda route: route.fulfill(status=200, content_type="application/json", body=answer))
    if kiosk:
        ctx.add_init_script(_KIOSK)
    if zoom:
        ctx.add_init_script(_ZOOM)
    page = ctx.new_page()
    page.goto(base + path, wait_until="load")
    page.locator(".si-ic").nth(len(items) - 1).wait_for(state="attached", timeout=15000)
    page.wait_for_timeout(300)
    return ctx, page


def _assert_clear(got):
    assert got["exitInView"], got
    assert got["exitOnTop"], got
    assert got["icons"] and all(i["onTop"] and i["inView"] for i in got["icons"]), got
    assert not any(i["overExit"] for i in got["icons"]), got


def _tap_exit(page, device):
    """Tap the middle of cook mode's Exit and say what the tap landed on. The
    note is kept in sessionStorage, which outlives the page Exit may leave."""
    page.evaluate("""() => {
      sessionStorage.setItem('tapped', '');
      document.addEventListener('click', function (ev) {
        if (!sessionStorage.getItem('tapped')) sessionStorage.setItem('tapped', String(ev.target.className));
      }, true);
    }""")
    box = page.locator(".cm-exit").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    if device.get("has_touch"):
        page.touchscreen.tap(x, y)
    else:
        page.mouse.click(x, y)
    page.wait_for_timeout(400)
    return page.evaluate("() => sessionStorage.getItem('tapped')")


@pytest.mark.parametrize("device", [ECHO_SHOW, THINKSMART], ids=["echo-show-8", "thinksmart-view"])
@pytest.mark.parametrize("kiosk", [False, True], ids=["", "kiosk-latched"])
def test_a_tablets_cook_page_shows_the_icons_beside_exit(browser, icons_on, app_server, device,
                                                         kiosk):
    # A tablet that has been in the whole interface has kiosk mode latched
    # (ui/start?kiosk=1): that is where the icons used to land on Exit.
    ctx, page = _open(browser, app_server, "/screen/cook", device, kiosk=kiosk)
    try:
        got = page.evaluate(_LAYOUT)
        assert got["inCookBar"], got
        _assert_clear(got)
        assert "cm-exit" in _tap_exit(page, device)
    finally:
        ctx.close()


@pytest.mark.parametrize("device,zoom", [(ECHO_SHOW, False), (THINKSMART, False),
                                         (BANDIT, True), (DESKTOP, False)],
                         ids=["echo-show-8", "thinksmart-view", "bandit", "desktop"])
def test_on_the_line_moves_the_icons_into_cook_mode_and_back(browser, icons_on, app_server,
                                                             device, zoom):
    ctx, page = _open(browser, app_server, "/ui/current-recipe?cook=1", device,
                      kiosk=True, zoom=zoom)
    try:
        got = page.evaluate(_LAYOUT)
        assert got["cookOpen"] and got["inCookBar"], got
        _assert_clear(got)
        assert "cm-exit" in _tap_exit(page, device)
        # Exit closed cook mode: the icons are back in the page's own bar.
        page.wait_for_timeout(300)
        back = page.evaluate("""() => {
          var row = document.querySelector('.si-row'), ic = row.querySelector('.si-ic');
          var r = ic.getBoundingClientRect();
          var hit = document.elementFromPoint((r.left + r.right) / 2, (r.top + r.bottom) / 2);
          return {cook: document.querySelector('.cm-top').offsetHeight > 0,
                  inNav: !!row.closest('nav.navbar'), onTop: !!hit && (hit === ic || ic.contains(hit))};
        }""")
        assert back == {"cook": False, "inNav": True, "onTop": True}, back
    finally:
        ctx.close()


def test_the_left_corner_puts_them_before_the_recipe_name(browser, icons_on, app_server):
    icons_on(status_icons_position="top_left")
    try:
        ctx, page = _open(browser, app_server, "/ui/current-recipe?cook=1", THINKSMART, kiosk=True)
        try:
            got = page.evaluate(_LAYOUT)
            assert got["inCookBar"] and got["firstInBar"], got
            _assert_clear(got)
        finally:
            ctx.close()
    finally:
        icons_on(status_icons_position="top_right")


def test_too_many_for_the_bar_wrap_below_it_inside_cook_mode(browser, icons_on, app_server):
    ctx, page = _open(browser, app_server, "/screen/cook", THINKSMART, TWELVE)
    try:
        got = page.evaluate(_LAYOUT)
        assert not got["inCookBar"], got
        assert page.evaluate("() => document.querySelector('.si-row').parentNode.id") == "sc-cook"
        _assert_clear(got)
    finally:
        ctx.close()


def test_two_icons_never_push_a_tablet_pages_exit_off_the_screen(browser, icons_on, app_server):
    """On a ThinkSmart View two icons fit in the bar only by pushing Exit past
    the edge; they go just below the bar instead, as three already did."""
    ctx, page = _open(browser, app_server, "/screen/?screen=ks-test", THINKSMART, THREE[:2])
    try:
        got = page.evaluate(_LAYOUT)
        assert got["exit"] == "sc-btn sc-exit", got
        _assert_clear(got)
    finally:
        ctx.close()


def test_an_icon_pops_in_once_not_on_every_poll(browser, icons_on, app_server):
    ctx, page = _open(browser, app_server, "/screen/cook", ECHO_SHOW)
    try:
        page.wait_for_timeout(800)
        # A poll redraws the row, as one does every few seconds.
        with page.expect_response(lambda r: r.url.endswith("/status-icons")):
            page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_timeout(100)
        running = page.evaluate(
            "() => document.getAnimations().filter(function (a) { return a.animationName === 'si-in'; }).length")
        assert running == 0
    finally:
        ctx.close()
