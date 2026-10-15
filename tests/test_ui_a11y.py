"""Accessibility and layout guards for the shared page chrome and the pages'
dialogs and form fields: the demo banner's measured offset, control labels,
named close buttons, keyboard focus, the overflowing top nav, contrast
tints, and the setup wizard's keyboard path.

Most of these are browser behaviours checked by eye in headless Chromium;
the tests here pin the markup and the script hooks those behaviours rely on
so a later edit cannot quietly drop them."""
from __future__ import annotations

import re
from pathlib import Path

_APP = Path(__file__).resolve().parents[1] / "service" / "app"
TEMPLATES = _APP / "templates"
STATIC = _APP / "static"


def _read(rel: str) -> str:
    return (_APP / rel).read_text(encoding="utf-8")


# ui1: demo banner height is measured, not hardcoded


def test_demo_banner_offset_is_measured_not_hardcoded():
    tpl = _read("templates/_demo_banner.html")
    assert "1.75rem" not in tpl
    assert "var(--pr-demo-banner-h" in tpl
    # kiosk.css pins the navbar top with !important, so the demo rule must too.
    assert re.search(r"body\.demo-banner-on \.navbar\.fixed-top \{\s*top: calc\("
                     r"var\(--kiosk-inset-top, 0px\) \+ var\(--pr-demo-banner-h, 0px\)\) !important;", tpl)
    assert "pr-demo-spacer" in tpl
    js = _read("static/js/demo-banner.js")
    assert "offsetHeight" in js
    assert "--pr-demo-banner-h" in js
    assert "addEventListener('resize'" in js
    assert "ResizeObserver" in js


# ui2: every visible field has a name


def _labels_missing_for(text: str) -> list[str]:
    """Labels that sit right before a control with an id but do not point at
    it with for=, so a screen reader announces the field without its name."""
    found = []
    for m in re.finditer(r"<label\b([^>]*)>(.*?)</label>", text, re.S):
        attrs, inner = m.group(1), m.group(2)
        if "for=" in attrs or re.search(r"<(input|select|textarea)\b", inner):
            continue
        if "toggle(" in inner:
            continue
        rest = text[m.end():m.end() + 600]
        nxt = rest.find("<label")
        if nxt >= 0:
            rest = rest[:nxt]
        ctl = re.search(r"<(input|select|textarea)\b[^>]*?\bid=\"([^\"{]+)\"|secret_input\('([^']+)'", rest)
        if ctl:
            found.append(ctl.group(2) or ctl.group(3))
    return found


def test_labels_point_at_their_fields():
    files = sorted(TEMPLATES.glob("*.html")) + sorted((TEMPLATES / "setup").glob("*.html"))
    missing = {f.name: _labels_missing_for(f.read_text(encoding="utf-8")) for f in files}
    assert {k: v for k, v in missing.items() if v} == {}


def test_secret_input_can_be_named_by_a_row_title():
    macros = _read("templates/setup/_macros.html")
    assert "macro secret_input(id, placeholder, label_id='')" in macros
    assert 'aria-labelledby="{{ label_id }}"' in macros
    assert "macro srow(name, hint='', tip='', name_id='')" in macros
    for rel in ("setup/_pane_scanning.html", "setup/_pane_inventory.html", "setup/_pane_security.html"):
        text = (TEMPLATES / rel).read_text(encoding="utf-8")
        for sid in re.findall(r"label_id='([^']+)'", text):
            assert f"name_id='{sid}'" in text, (rel, sid)
    # Every secret field outside a for= label is named through label_id.
    for f in (TEMPLATES / "setup").glob("*.html"):
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r"secret_input\('([^']+)'[^)]*\)", text):
            if "label_id=" in m.group(0):
                continue
            assert f'for="{m.group(1)}"' in text, (f.name, m.group(1))


def test_unlabeled_page_controls_have_names():
    pending = _read("static/js/pages/pending.js")
    for name in ("Name", "Quantity", "Unit", "Category", "Storage", "Best by"):
        assert f'aria-label="{name}"' in pending
    assert pending.count("d-md-none") >= 6
    inv = _read("templates/inventory.html")
    assert 'for="editCategory"' in inv and 'for="editBestBy"' in inv
    assert 'for="expiring-days"' in _read("templates/expiring.html")
    journal = _read("templates/journal.html")
    assert 'id="limit-select" style="width:auto" aria-label=' in journal
    assert 'id="filter-input" aria-label=' in journal
    assert 'for="cr-servings"' in _read("templates/current-recipe.html")
    assert 'for="vision_provider"' in _read("templates/setup/_wizard.html")
    defaults = _read("templates/defaults.html")
    for fid in ("filterCategory", "filterStorage", "filterSearch"):
        assert re.search(rf'id="{fid}" aria-label="', defaults)


# ui3: close buttons and dialogs have names


def _all_markup() -> list[tuple[str, str]]:
    files = list(TEMPLATES.rglob("*.html")) + [
        p for p in (STATIC / "js").rglob("*.js") if "vendor" not in p.parts]
    return [(str(p.relative_to(_APP)), p.read_text(encoding="utf-8")) for p in files]


def test_every_close_button_has_a_name():
    bad = []
    for rel, text in _all_markup():
        for m in re.finditer(r"<button\b[^>]*class=\"btn-close[^\"]*\"[^>]*>", text):
            if "aria-label=" not in m.group(0):
                bad.append(rel)
    assert bad == []


def test_every_modal_is_labelled_by_its_title():
    bad = []
    for rel, text in _all_markup():
        for m in re.finditer(r"<div class=\"modal fade\"[^>]*>", text):
            lab = re.search(r'aria-labelledby="([^"]+)"', m.group(0))
            if not lab or f'id="{lab.group(1)}"' not in text:
                bad.append((rel, m.group(0)[:60]))
    assert bad == []
    assert "modal.setAttribute('aria-labelledby', 'cam-preview-title')" in _read("static/js/setup/deck-editor.js")


# ui4: keyboard focus is visible and the page has a skip link


def test_focus_ring_and_skip_link():
    css = _read("static/css/base.css")
    assert ".subpill-btn:focus-visible" in css
    assert ".navbar .nav-link:focus-visible" in css
    assert re.search(r"outline: 2px solid var\(--pr-pink-soft", css)
    base = _read("templates/base.html")
    body = base.index("<body>")
    skip = base.index('class="visually-hidden-focusable pr-skip-link"')
    assert skip > body and skip < base.index("<nav class=\"navbar")
    assert '<main class="container-fluid px-4" id="pageContent" tabindex="-1"' in base
    assert "</main>" in base
    js = _read("static/js/base-page.js")
    assert "closest('.pr-skip-link')" in js and "main.focus()" in js


# ui5: tabs that do not fit the top row go to the More menu


def test_top_nav_overflow_is_measured():
    base = _read("templates/base.html")
    assert 'src="static/js/nav-overflow.js?v={{ app_version }}" defer' in base
    # The More menu still lists every page, so a hidden tab stays reachable.
    assert "{% for tab in nav_tabs() if tab.key not in ['about', 'defaults'] and not tab.heading" in base
    js = _read("static/js/nav-overflow.js")
    assert "scrollWidth > row.clientWidth" in js
    assert "addEventListener('resize'" in js
    assert "pr-nav-overflow" in js and "pr-more-has-tabs" in js
    css = _read("static/css/base.css")
    assert ".navbar .nav-primary .nav-item.pr-nav-overflow { display: none; }" in css
    # Number-key shortcuts keep counting a tab hidden for want of room.
    assert "closest('.pr-nav-overflow')" in _read("static/js/keyboard-nav.js")


# ui6: the brand theme's text colours pass WCAG AA


def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    chans = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in chans]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _ratio(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def test_theme_retints_pass_contrast():
    css = _read("static/vendor/themes/pantryraider.css")
    body, tertiary = "#1b1e22", "#282d33"
    assert "--bs-btn-color: #b4bcc4;" in css
    assert _ratio("#b4bcc4", body) >= 4.5 and _ratio("#b4bcc4", tertiary) >= 4.5
    assert re.search(r"\.btn-outline-success \{\s*--bs-btn-color: var\(--pr-green\);", css)
    assert _ratio("#37c98a", body) >= 4.5 and _ratio("#37c98a", tertiary) >= 4.5
    assert ".badge.bg-secondary { color: #16181c; }" in css
    assert _ratio("#16181c", "#949ca4") >= 4.5
    assert ".badge.bg-danger { color: #1c0606; }" in css
    assert _ratio("#1c0606", "#f26a6a") >= 4.5
    assert "--bs-link-color: #ff6aa8;" in css
    assert _ratio("#ff6aa8", tertiary) >= 4.5
    # The setup cards and wizard steps sit on the same tertiary surface.
    assert re.search(r"\.bg-body-tertiary,\s*\.section-card,\s*\.wiz-card,", css)
    assert ".alert .form-text { color: inherit; }" in css


# ui7: the setup wizard works from the keyboard


def test_install_mode_cards_are_a_radio_group():
    wiz = _read("templates/setup/_wizard.html")
    assert 'role="radiogroup" aria-label=' in wiz and 'id="install-mode-group"' in wiz
    assert 'role="radio" aria-checked=' in wiz
    assert "tabindex=\"{{ '0' if _sel or (not _any_mode and loop.first) else '-1' }}\"" in wiz
    js = _read("static/js/setup/wizard.js")
    assert "c.setAttribute('aria-checked', on ? 'true' : 'false');" in js
    assert "initInstallModeKeys" in js and "'ArrowRight'" in js and "' '" in js
    # Each step change moves focus to the new step's heading.
    show = js[js.index("function _wizShowStep(n)"):]
    show = show[:show.index("\n}\n")]
    assert "' .wiz-card-title'" in show and "title.focus(" in show
    assert ".install-card:focus-visible" in _read("templates/setup.html")


# ui8: closing a dialog puts focus back on what opened it


def test_dialogs_return_focus_to_their_opener():
    js = _read("static/js/lib/dom.js")
    assert "doc.addEventListener('show.bs.modal'" in js
    assert "doc.addEventListener('hidden.bs.modal'" in js
    assert "ev.relatedTarget || doc.activeElement" in js
    # A list re-rendered behind the dialog: the same action takes focus.
    assert "getAttribute('data-on-click') === rec.action" in js
    # dom.js loads on base.html pages and the standalone setup page alike.
    for page in ("base.html", "setup.html"):
        assert "static/js/lib/dom.js" in (TEMPLATES / page).read_text(encoding="utf-8")


# ui9: the settings help icons have a name


def test_info_icon_has_a_name():
    macros = _read("templates/setup/_macros.html")
    icon = macros[macros.index("{% macro info(text)"):]
    icon = icon[:icon.index("endmacro")]
    assert 'role="button" tabindex="0" aria-label="More information"' in icon


# ui10: the "No password" notice stays out of the first-run wizard


def _setup_page(monkeypatch, tmp_path, *, configured: bool, **banner) -> str:
    import os
    import sys

    service = _APP.parent
    sys.path.insert(0, str(service))
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app
    from app.services import access

    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(settings, "demo_mode", False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://g" if configured else "")
    monkeypatch.setattr(settings, "grocy_api_key", "k" if configured else "")
    ctx = {"open_notice": False, "grace_until": None, "grace_address": "",
           "grace_kind": None, "gateway_peer": False}
    ctx.update(banner)
    monkeypatch.setattr(access, "banner_context", lambda request: dict(ctx))
    cwd = os.getcwd()
    os.chdir(service)
    try:
        r = TestClient(app, client=("192.168.1.20", 50000)).get("/setup")
    finally:
        os.chdir(cwd)
    assert r.status_code == 200
    return r.text


def test_open_notice_hidden_in_the_wizard(monkeypatch, tmp_path):
    page = _setup_page(monkeypatch, tmp_path, configured=False, open_notice=True)
    assert 'id="wiz-step-1"' in page
    assert 'id="pr-open-notice"' not in page
    assert 'id="pr-access-banner"' not in page


def test_grace_banner_still_shows_in_the_wizard(monkeypatch, tmp_path):
    page = _setup_page(monkeypatch, tmp_path, configured=False, open_notice=True,
                       grace_until="2026-10-14T00:00:00+00:00", grace_kind="proxied",
                       grace_address="pantry.example.com")
    assert 'id="pr-grace-banner"' in page
    assert 'id="pr-open-notice"' not in page


def test_open_notice_still_shows_in_settings(monkeypatch, tmp_path):
    page = _setup_page(monkeypatch, tmp_path, configured=True, open_notice=True)
    assert 'id="settings-root"' in page
    assert 'id="pr-open-notice"' in page
