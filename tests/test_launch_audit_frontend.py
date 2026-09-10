"""Launch-audit fixes on the browser surfaces (templates + static JS).

Every guard here is a static read of the shipped files, so the suite stays
pure logic: no browser, no network, no Docker.

What is pinned:
  * the Start page's own script blocks wait for the deferred status script,
    so the consolidated single poll is not silently undone by the fallbacks;
  * the phone QR dialog fetches nothing until it is opened;
  * one shared helper turns a failed request into a readable sentence, and no
    page still shows a raw JSON-parse complaint instead;
  * shopping and review actions say when they did not work, in a slot of their
    own, instead of failing in silence or blanking the list;
  * Clear on the cooking page asks first;
  * daily screens use the app's own vocabulary, not the inventory engine's name;
  * white text sits on a pink that actually passes WCAG AA;
  * the setup wizard will not walk past the Security step with no password;
  * restoring a backup from another device says what it does and does not bring.
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_T = _ROOT / "service" / "app" / "templates"
_S = _ROOT / "service" / "app" / "static"


def _read(rel: str) -> str:
    base = _S if rel.startswith(("js/", "vendor/")) else _T
    return (base / rel).read_text(encoding="utf-8")


# --------------------------------------------------------------- script order


def test_start_page_defers_the_status_script():
    start = _read("start.html")
    tag = re.search(r"<script[^>]*kiosk-status\.js[^>]*>", start)
    assert tag is not None and "defer" in tag.group(0)


def test_start_page_blocks_wait_for_the_document():
    """Whole blocks, not just the `if (window.PRKioskStatus)` branches.

    A partial wrap leaves the else-branch pollers racing the subscribe path,
    which puts the Start page back to one request per count.
    """
    start = _read("start.html")
    assert "function prOnReady(fn)" in start
    # The three page blocks that touch PRKioskStatus are wrapped whole.
    assert start.count("prOnReady(function () {") == 3
    # No block still runs its body at parse time.
    assert "  <script>\n    (function () {" not in start


def test_start_page_presence_indicator_waits_for_the_status_script():
    """It reads PRKioskStatus, which is deferred here now, so it must be too."""
    tag = re.search(r"<script[^>]*presence-indicator\.js[^>]*>", _read("start.html"))
    assert tag is not None and "defer" in tag.group(0)


def test_osk_script_is_deferred():
    tag = re.search(r"<script[^>]*osk\.js[^>]*>", _read("_osk.html"))
    assert tag is not None and "defer" in tag.group(0)


# ------------------------------------------------------------------ QR dialog


def test_qr_dialog_loads_nothing_until_it_is_opened():
    base = _read("base.html")
    img = re.search(r'<img id="qr-img"[^>]*>', base)
    assert img is not None, "the QR image lost its id"
    assert 'data-src="ui/qr"' in img.group(0)
    assert " src=" not in img.group(0), "a hidden image with src is still fetched"
    # And the address lookup is inside the open handler, not at page load.
    modal_js = base.split("var modal = document.getElementById('qrModal');", 1)[1]
    assert "show.bs.modal" in modal_js.split("</script>", 1)[0]
    assert base.count("fetch('ui/qr/url')") == 1


# -------------------------------------------------------------- error wording


def test_shared_error_helper_exists():
    js = _read("js/err-text.js")
    assert "window.prErrText" in js and "window.prReason" in js
    # It never lets a JSON-parse complaint reach the user.
    assert "catch (e) { /* not JSON, fall through */ }" in js
    assert "HTTP" in js
    base = _read("base.html")
    tag = re.search(r"<script[^>]*err-text\.js[^>]*>", base)
    assert tag is not None, "base.html does not load the helper"
    assert "defer" not in tag.group(0), "a page that loads its list on the way past needs it"
    # First in the body, not in the head: every head script but kiosk-display.js
    # is deferred there, and this one must not be.
    assert base.index(tag.group(0)) > base.index("</head>")
    assert base.index(tag.group(0)) < base.index("<nav class=\"navbar")


def test_no_page_still_shows_a_json_parse_complaint():
    """`(await r.json()).detail` rejects outright on an HTML or plain-text
    error page, so the banner used to read 'Unexpected token < in JSON'."""
    unguarded = re.compile(r"throw new Error\(\(await \w+\.json\(\)\)\.detail")
    offenders = []
    for path in sorted(list(_T.rglob("*.html")) + list((_S / "js").rglob("*.js"))):
        if path.name == "err-text.js":
            continue        # the helper quotes the pattern it replaced
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in unguarded.finditer(text):
            line = text[: m.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(_ROOT)}:{line}")
    assert not offenders, "unguarded r.json() in an error path: " + ", ".join(offenders)


def test_no_stringified_error_object_reaches_the_user():
    """'Import failed: ' + e renders as 'TypeError: Failed to fetch'."""
    for rel in ("cook.html", "convert.html", "current-recipe.html", "calibrate.html"):
        text = _read(rel)
        assert "alert('Failed: ' + e.message)" not in text
        assert "'Request failed: ' + e" not in text
        assert "'<span class=\"text-danger\">' + e + '</span>'" not in text
    assert "'Import failed: ' + e" not in _read("js/manage-pantry.js")


# ---------------------------------------------------------------- shopping UI


def test_shopping_actions_report_failure_without_wiping_the_list():
    s = _read("shopping.html")
    # A message slot of its own, above the items container.
    assert 'id="shopping-msg"' in s
    assert s.index('id="shopping-msg"') < s.index('<div id="items"')
    # Every mutating handler checks the response now.
    for fn in ("toggleItem", "removeItem", "addItem", "clearDone", "addSuggestion"):
        body = s.split(f"async function {fn}(", 1)[1].split("\nasync function", 1)[0]
        assert "if (!r.ok) throw new Error(await prErrText(r));" in body, fn
        assert "shoppingMsg(" in body, fn
    # The typed item survives a failure: it is only cleared on the success path.
    add = s.split("async function addItem(", 1)[1].split("\n// Typeahead", 1)[0]
    assert add.index("if (!r.ok) throw") < add.index("input.value = '';")


def test_shopping_add_button_has_a_name():
    assert 'aria-label="Add this item to the list"' in _read("shopping.html")


# ------------------------------------------------------------------ review UI


def test_review_page_survives_a_reply_that_is_not_json():
    p = _read("pending.html")
    body = p.split("async function loadPending() {", 1)[1].split("\n}", 1)[0]
    assert "try {" in body and "catch (e)" in body
    assert "r.json().catch(() => ({}))" in body
    # A transient poll failure leaves the rows alone and says so in the notice.
    assert "pendingNotice(" in body
    assert 'id="pending-notice"' in p
    assert p.index('id="pending-notice"') < p.index('<div id="pending-list">')


# ------------------------------------------------------------- destructive UI


def test_clear_on_the_cooking_page_asks_first():
    c = _read("current-recipe.html")
    body = c.split("async function clearCurrentRecipe(btn) {", 1)[1].split("\n}", 1)[0]
    assert "confirm(" in body
    assert "Its timers keep running." in body


# ----------------------------------------------------------------- vocabulary


def test_daily_screens_do_not_name_the_inventory_engine():
    """Settings and the config-error hints still say Grocy; the screens a cook
    uses every day speak the app's own vocabulary."""
    checks = {
        "pending.html": ("Commit All to Grocy", "item(s) to Grocy", "stock in Grocy"),
        "add.html": ("Import All to Grocy", "your Grocy stock", "written back to Grocy"),
        "audit.html": ("count differs from Grocy", "what Grocy expects", "Set Grocy's stock"),
        "cook.html": ("your Grocy inventory",),
        "shopping.html": ("in Grocy's price history", "price(s) in Grocy"),
        "js/manage-pantry.js": ("item(s) to Grocy",),
    }
    offenders = []
    for rel, phrases in checks.items():
        text = _read(rel)
        # Strip JS line comments so an explanatory note is not mistaken for copy.
        stripped = re.sub(r"^\s*//.*$", "", text, flags=re.M)
        offenders += [f"{rel}: {p}" for p in phrases if p in stripped]
    assert not offenders, "builder vocabulary on a daily screen: " + ", ".join(offenders)


def test_stale_settings_breadcrumbs_point_at_the_real_panes():
    assert "This Device &rarr; Start Page" in _read("start.html")
    assert "Personalization &rarr; Start Page" not in _read("start.html")
    cr = _read("current-recipe.html")
    assert "Settings > Kitchen > Recipe suggestions" in cr
    assert "Recipe Preferences" not in cr


# -------------------------------------------------------------------- contrast


def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    out = 0.0
    for chan, weight in zip((h[0:2], h[2:4], h[4:6]), (0.2126, 0.7152, 0.0722)):
        c = int(chan, 16) / 255
        c = c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        out += c * weight
    return out


def _ratio(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def test_white_on_pink_passes_wcag_aa():
    css = _read("vendor/themes/pantryraider.css")
    assert "--pr-pink-deep: #d80062;" in css
    assert _ratio("#ffffff", "#d80062") >= 4.5
    assert _ratio("#ffffff", "#b80054") >= 4.5
    # The brand pink itself does not pass for text, so it must not carry any.
    assert _ratio("#ffffff", "#F2006E") < 4.5
    for rule in (
        ".btn-primary {\n  background-color: var(--pr-pink-deep);",
        ".btn-outline-primary:hover {\n  background-color: var(--pr-pink-deep);",
        ".text-bg-primary   { background-color: var(--pr-pink-deep)",
    ):
        assert rule in css, rule
    # ... and the demo banner paints the same surface inline.
    assert "background:#d80062;color:#fff" in _read("_demo_banner.html")
    # The brand pink stays for borders, links and focus rings.
    assert "--pr-pink:      #F2006E;" in css


def test_icon_only_buttons_have_names():
    assert _read("convert.html").count('aria-label="Remove this conversion"') == 2


# ---------------------------------------------------------------- setup wizard


def test_wizard_will_not_walk_past_an_empty_password():
    js = _read("js/setup/wizard.js")
    assert "function wizCheckPasswordSet()" in js
    nxt = js.split("function wizNext() {", 1)[1].split("\nfunction wizBack", 1)[0]
    assert "if (_wizStep === 2 && !wizCheckPasswordSet())" in nxt
    # Turning authentication off, or a password stored before, still passes.
    setter = js.split("function wizCheckPasswordSet() {", 1)[1].split("\n}", 1)[0]
    assert "auth_required" in setter and "_wizHasPassword()" in setter
    assert "pi_remote" in setter
    # The inline note it toggles is on the step itself.
    assert 'id="wiz-pw-required"' in _read("setup/_wizard.html")


# --------------------------------------------------------------- restore copy


def test_restore_copy_says_what_a_foreign_backup_does_not_bring():
    pane = _read("setup/_pane_backups.html")
    assert pane.count("not what that device is") == 2
    assert pane.count("keeps its own setup type") == 2
    js = _read("js/setup/panes.js")
    assert js.count("keeps its own setup type") == 2      # the two confirms
    assert js.count("if (d.identity_kept)") == 2          # the two result lines
    # User-forward: the two restore rows name no internal deployment terms.
    rows = [b.split("%}", 1)[0] for b in pane.split("{% call srow('Restore")[1:]]
    assert len(rows) == 2
    for row in rows:
        for banned in ("pi_remote", "pi_hosted", "deployment_mode", "satellite", "Grocy"):
            assert banned not in row, banned


# ------------------------------------------------------------------ phone fit


def test_expiring_header_reflows_on_a_phone():
    e = _read("expiring.html")
    assert "flex-wrap gap-2 my-3" in e
    assert 'id="expiring-actions"' in e
    assert "@media (max-width: 575.98px)" in e


def test_subnav_pills_scroll_instead_of_truncating_on_a_phone():
    base = _read("base.html")
    phone = base.split("@media (max-width: 767.98px) {", 1)[1].split("\n    }", 1)[0]
    assert "overflow-x: auto" in phone
    assert ".subpill-btn { flex: 0 0 auto;" in phone
