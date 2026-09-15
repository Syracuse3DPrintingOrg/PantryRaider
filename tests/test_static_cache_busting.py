"""Our own static assets must be cache-busted (FoodAssistant-9k2v).

A kiosk browser caches aggressively and lives for months without anyone
clearing it. A script tag with no ?v= means the panel keeps running whatever
copy it first saw, so later fixes to that file never arrive: this is how an
already-fixed layout bug came back on the Bandit weeks later. setup.html,
login.html, pin.html, and getting-ready.html are standalone templates (they do
not extend base.html) and each had to be given the buster by hand; this test is
the guard, because the failure is invisible until real hardware is in the wrong
state.

Vendor assets are exempt: they change only when their filename changes.
"""
import pathlib
import re

TEMPLATES = pathlib.Path(__file__).resolve().parents[1] / "service/app/templates"

# src="..." / href="..." pointing at our own static js or css.
REF = re.compile(r'(?:src|href)="(static/(?:js|css)/[^"]+\.(?:js|css))(\?[^"]*)?"')


def test_every_local_static_asset_is_cache_busted():
    missing = []
    for tpl in TEMPLATES.rglob("*.html"):
        for m in REF.finditer(tpl.read_text()):
            path, query = m.group(1), m.group(2) or ""
            if "v=" not in query:
                missing.append(f"{tpl.relative_to(TEMPLATES)}: {path} has no ?v= cache buster")
    assert not missing, (
        "a kiosk will cache these forever and never see a fix to them:\n  "
        + "\n  ".join(missing))


def test_the_kiosk_display_script_is_busted_everywhere_it_loads():
    """The panel's scale, rotation, and safe-area insets all come from this one
    file, so a stale copy is exactly the bug that keeps coming back."""
    for tpl in TEMPLATES.rglob("*.html"):
        src = tpl.read_text()
        if "kiosk-display.js" not in src:
            continue
        for m in re.finditer(r'src="static/js/kiosk-display\.js(\?[^"]*)?"', src):
            assert m.group(1) and "v=" in m.group(1), (
                f"{tpl.relative_to(TEMPLATES)} loads kiosk-display.js with no ?v=; "
                "the panel will keep running the version it first cached")
