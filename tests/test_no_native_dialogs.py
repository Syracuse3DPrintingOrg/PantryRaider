"""No browser alert(), confirm() or prompt() boxes; the page asks itself.

A browser box is titled with the page's address, so on a kiosk every question
arrives as "127.0.0.1 says", and a prompt() cannot be typed into at all: the
on-screen keyboard (static/js/osk.js) only reaches fields in the page. Pages
use PR.ask, PR.confirmBox, PR.notice and PR.confirmSubmit from
static/js/lib/dialog.js instead. This sweeps every script and template for a
call to one of the three browser functions, and checks that every page using
the replacements loads the file that defines them.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "service" / "app"
TEMPLATES = APP / "templates"
JS = APP / "static" / "js"
DIALOG = JS / "lib" / "dialog.js"

# confirm( or window.confirm( and friends, but not x.confirm(, resolve_confirm(
# or confirmBox(.
_NATIVE = re.compile(r"(?<![\w.$])(?:(?:window|globalThis|self|top|parent)\s*\.\s*)?(?:alert|confirm|prompt)\s*\(")
_JS_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
# A // comment at the start of a line or after whitespace, so a URL inside a
# string ('http://x') is left alone.
_JS_LINE_COMMENT = re.compile(r"(?:^|(?<=\s))//.*$", re.M)
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_USES_DIALOG = re.compile(r"\bPR\.(?:ask|confirmBox|notice|confirmSubmit)\s*\(")


def _scripts() -> list[Path]:
    return sorted(p for p in JS.rglob("*.js") if "vendor" not in p.relative_to(JS).parts)


def _templates() -> list[Path]:
    return sorted(TEMPLATES.rglob("*.html"))


def _strip_js(text: str) -> str:
    keep_lines = lambda m: "\n" * m.group(0).count("\n")  # noqa: E731
    return _JS_LINE_COMMENT.sub("", _JS_BLOCK_COMMENT.sub(keep_lines, text))


def _strip_html(text: str) -> str:
    keep_lines = lambda m: "\n" * m.group(0).count("\n")  # noqa: E731
    return _HTML_COMMENT.sub(keep_lines, _JINJA_COMMENT.sub(keep_lines, text))


def _calls(text: str) -> list[int]:
    return [text.count("\n", 0, m.start()) + 1 for m in _NATIVE.finditer(text)]


def test_the_sweep_sees_what_it_should():
    assert _calls("if (!confirm('x')) return;")
    assert _calls("window.alert(msg)")
    assert _calls("const v = window.prompt('a', b);")
    assert _calls('data-on-submit="return confirm(\'Delete?\')"')
    assert not _calls("PR.confirmBox('x')")
    assert not _calls("runner.resolve_confirm(x)")
    assert not _calls("askCooked(); confirmCooked(this)")
    assert not _calls(_strip_js("// call confirm() here\nx();"))
    assert not _calls(_strip_js("/* alert(1) */ x();"))
    assert _calls(_strip_js("var u = 'http://x'; alert(u);"))


def test_scripts_call_no_browser_dialogs():
    bad = []
    for path in _scripts():
        if path == DIALOG:
            continue
        text = _strip_js(path.read_text(encoding="utf-8"))
        bad += [f"{path.relative_to(APP)}:{n}" for n in _calls(text)]
    assert not bad, (
        "use PR.ask / PR.confirmBox / PR.notice (static/js/lib/dialog.js) instead of "
        "the browser's prompt / confirm / alert:\n" + "\n".join(bad))


def test_templates_call_no_browser_dialogs():
    bad = []
    for path in _templates():
        text = _strip_html(path.read_text(encoding="utf-8"))
        bad += [f"{path.relative_to(APP)}:{n}" for n in _calls(text)]
    assert not bad, (
        "use PR.confirmSubmit(this, ..., event) for a question before a submit:\n"
        + "\n".join(bad))


def test_dom_helpers_no_longer_let_confirm_through():
    dom = (JS / "lib" / "dom.js").read_text(encoding="utf-8")
    assert "NATIVE_OK" not in dom


def _extends_base(text: str) -> bool:
    return bool(re.search(r"""\{%-?\s*extends\s+["']base\.html["']""", text))


def test_pages_that_ask_load_the_dialog_script():
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "static/js/lib/dialog.js" in base
    # dom.js and dialog.js travel together: every page with one has both,
    # dialog.js second (it adds to the PR namespace dom.js makes).
    for path in _templates():
        text = path.read_text(encoding="utf-8")
        if "static/js/lib/dom.js" in text:
            assert "static/js/lib/dialog.js" in text, path.name
            assert text.index("static/js/lib/dom.js") < text.index("static/js/lib/dialog.js"), path.name

    users = {p.relative_to(JS).as_posix() for p in _scripts()
             if p != DIALOG and _USES_DIALOG.search(p.read_text(encoding="utf-8"))}
    assert users, "nothing uses the dialog any more?"
    by_root = {}
    for path in _templates():
        text = path.read_text(encoding="utf-8")
        for rel in users:
            if f"static/js/{rel}" in text:
                by_root.setdefault(rel, []).append(path)
    for rel, pages in by_root.items():
        for page in pages:
            text = page.read_text(encoding="utf-8")
            if page.parent.name == "setup" or page.name.startswith("_"):
                continue    # a partial, rendered inside a page checked here
            assert _extends_base(text) or "static/js/lib/dialog.js" in text, (
                f"{page.name} loads {rel}, which asks through PR.*, without dialog.js")
    # Templates that ask before a submit, straight from markup.
    for path in _templates():
        text = path.read_text(encoding="utf-8")
        if "PR.confirmSubmit(" in text and not path.name.startswith("_"):
            assert _extends_base(text) or "static/js/lib/dialog.js" in text, path.name
