"""No inline script anywhere, so the CSP can refuse it.

The Content-Security-Policy's script-src is 'self' only. A browser under it
silently skips an inline <script> block and every on*= handler attribute, so
one of them slipping back into a template (or into markup a script builds)
means a button that does nothing. This scans the source for both and pins the
policy itself.

Allowed instead:

* <script src="..."> for code, from static/js;
* <script type="application/json"> for server data (never executed);
* data-on-click="fn(args)" and friends for controls, run by
  static/js/lib/dom.js.

The rendered-page side (every page, with hostile values planted) is in
test_rendered_js_syntax.py.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "service" / "app"
TEMPLATES = APP / "templates"
JS = APP / "static" / "js"
sys.path.insert(0, str(ROOT / "service"))

_SCRIPT_TAG = re.compile(r"<script\b([^>]*)>", re.I)
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_JS_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_JS_LINE_COMMENT = re.compile(r"^\s*//.*$", re.M)
# An event-handler attribute: on<event>= followed by a quote or a Jinja value.
# The lookbehind leaves data-on-click=, el.onclick = and on_change= alone.
_HANDLER_ATTR = re.compile(r"""(?<![\w.$-])on[a-z]+\s*=\s*(?:["'{]|\\["'])""", re.I)


def _templates() -> list[Path]:
    return sorted(TEMPLATES.rglob("*.html"))


def _scripts() -> list[Path]:
    return sorted(p for p in JS.rglob("*.js") if "vendor" not in p.relative_to(JS).parts)


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _inline_script_tags(text: str) -> list[tuple[int, str]]:
    found = []
    for m in _SCRIPT_TAG.finditer(text):
        attrs = m.group(1)
        if re.search(r"\bsrc\s*=", attrs, re.I):
            continue
        if re.search(r"""\btype\s*=\s*["']?application/json\b""", attrs, re.I):
            continue
        found.append((_line(text, m.start()), m.group(0)))
    return found


def test_templates_have_no_inline_script_blocks():
    bad = []
    for path in _templates():
        text = _HTML_COMMENT.sub("", _JINJA_COMMENT.sub("", path.read_text()))
        for line, tag in _inline_script_tags(text):
            bad.append(f"{path.relative_to(APP)}:{line}: {tag}")
    assert not bad, "inline <script> blocks the CSP will refuse:\n" + "\n".join(bad)


def test_scripts_build_no_inline_script_blocks():
    bad = []
    for path in _scripts():
        # Comments that mention <script> are prose, not markup.
        text = _JS_BLOCK_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), path.read_text())
        text = _JS_LINE_COMMENT.sub("", text)
        for line, tag in _inline_script_tags(text):
            bad.append(f"{path.relative_to(APP)}:{line}: {tag}")
    assert not bad, "markup with inline <script> the CSP will refuse:\n" + "\n".join(bad)


@pytest.mark.parametrize("kind", ["templates", "scripts"])
def test_no_on_handler_attributes(kind):
    paths = _templates() if kind == "templates" else _scripts()
    bad = []
    for path in paths:
        text = path.read_text()
        if kind == "templates":
            text = _JINJA_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)
        for m in _HANDLER_ATTR.finditer(text):
            bad.append(f"{path.relative_to(APP)}:{_line(text, m.start())}: {m.group(0)}")
    assert not bad, ("on*= handlers the CSP will refuse; use data-on-* instead:\n"
                     + "\n".join(bad))


def test_the_scanner_catches_what_it_should():
    """Pin the patterns, so a loosened regex cannot pass the scans vacuously."""
    assert _inline_script_tags('<script>alert(1)</script>')
    assert _inline_script_tags('<script type="module">x</script>')
    assert not _inline_script_tags('<script src="static/js/x.js"></script>')
    assert not _inline_script_tags('<script type="application/json" id="d">{}</script>')
    for hit in ('<a onclick="x()">', "<img onerror='x()'>", '<div onchange={{ h }}>',
                '`<b onclick=\\"x()\\">`'):
        assert _HANDLER_ATTR.search(hit), hit
    for miss in ('<a data-on-click="x()">', "el.onclick = fn;", "toggle(on_change='x')",
                 "const onload = () => 1;"):
        assert not _HANDLER_ATTR.search(miss), miss


def test_csp_refuses_inline_script():
    from app.main import _SECURITY_HEADERS

    csp = dict(_SECURITY_HEADERS)["Content-Security-Policy"]
    directives = {d.split()[0]: d.split()[1:] for d in csp.split(";") if d.strip()}
    assert directives["script-src"] == ["'self'"], directives["script-src"]
    assert "'unsafe-eval'" not in csp
