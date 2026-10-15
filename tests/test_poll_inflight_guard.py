"""Repeating polls must not stack requests or outlive the page.

setInterval fires on schedule whether or not the last request has answered,
so a slow server used to collect a queue of identical requests from one page.
A browser also keeps a page it has just left in its back/forward cache with
its requests still running and allows six connections to one site, so those
requests filled the connections the next page needed and froze a kiosk for
seconds (see tests/test_kiosk_long_poll_leave.py for the long-poll half).

Every setInterval that reaches the server goes through PR.poller
(static/js/lib/dom.js), which skips a tick while the last one is still going,
gives each request a deadline, and aborts it when the page is being left.
This sweep fails when a setInterval-driven fetch or image load skips it.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "service" / "app"
JS = APP / "static" / "js"
DOM_JS = JS / "lib" / "dom.js"
TEMPLATES = APP / "templates"
NODE = shutil.which("node")

# What counts as reaching the server from inside a poll.
NETWORK = re.compile(r"\bfetch\(|\bfetchJSON\(|XMLHttpRequest|new Image\(|\bloadImage\(|\.src\s*=[^=]")
GUARD = "PR.poller("
_IDENT = r"[A-Za-z_$][\w$]*"
_KEYWORDS = {"if", "for", "while", "switch", "return", "function", "catch", "typeof",
             "new", "await", "async", "setInterval", "setTimeout", "clearInterval"}


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    # A // inside a string (a URL) is preceded by a quote or a colon.
    return re.sub(r"(?<![:'\"`\\])//[^\n]*", "", text)


def _balanced(text: str, start: int, open_ch: str = "(", close_ch: str = ")") -> str:
    """The text from text[start] (an opening bracket) to its matching close."""
    depth = 0
    quote = None
    i = start
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "'\"`":
            quote = c
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    return text[start:]


def _first_arg(call_args: str) -> str:
    """The first argument of a call, given its parenthesised argument list."""
    inner = call_args[1:-1]
    depth = 0
    quote = None
    for i, c in enumerate(inner):
        if quote:
            if c == quote and inner[i - 1] != "\\":
                quote = None
        elif c in "'\"`":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            return inner[:i].strip()
    return inner.strip()


def _definitions(code: str) -> tuple[dict[str, str], set[str]]:
    """Function bodies by name, and the names bound to PR.poller(...)."""
    defs: dict[str, str] = {}
    guarded: set[str] = set()
    for m in re.finditer(r"\bfunction\s+(%s)\s*\(" % _IDENT, code):
        brace = code.find("{", m.end())
        if brace >= 0:
            defs.setdefault(m.group(1), _balanced(code, brace, "{", "}"))
    for m in re.finditer(r"\b(%s)\s*=\s*(?!=)" % _IDENT, code):
        name, rest = m.group(1), code[m.end():]
        if rest.startswith(GUARD):
            guarded.add(name)
            defs.setdefault(name, _balanced(code, m.end() + len("PR.poller"), "(", ")"))
            continue
        fn = re.match(r"(?:async\s+)?(?:function\b[^{]*|\([^)]*\)\s*=>\s*|%s\s*=>\s*)\{" % _IDENT, rest)
        if fn:
            defs.setdefault(name, _balanced(code, m.end() + fn.end() - 1, "{", "}"))
    return defs, guarded


def _calls(body: str) -> set[str]:
    names = set(re.findall(r"(?<![.\w$])(%s)\s*\(" % _IDENT, body))
    # A bare identifier handed to setInterval is called too.
    if re.fullmatch(_IDENT, body.strip()):
        names.add(body.strip())
    return names - _KEYWORDS


def _is_guarded(body: str, defs: dict[str, str], guarded: set[str]) -> bool:
    if GUARD in body:
        return True
    for name in _calls(body):
        if name in guarded or GUARD in defs.get(name, ""):
            return True
    return False


def _reaches_network(body: str, defs: dict[str, str], seen: set[str] | None = None) -> bool:
    if NETWORK.search(body):
        return True
    seen = seen if seen is not None else set()
    for name in _calls(body):
        if name in defs and name not in seen:
            seen.add(name)
            if _reaches_network(defs[name], defs, seen):
                return True
    return False


def _callback_text(path: Path, finding: str) -> str:
    """The full callback text for one finding (its line number)."""
    code = _strip_comments(path.read_text())
    line = int(finding.split(":")[0].split()[1])
    for m in re.finditer(r"\bsetInterval\s*\(", code):
        if code.count("\n", 0, m.start()) + 1 == line:
            return _first_arg(_balanced(code, m.end() - 1))
    return ""


def unguarded_polls(source: str) -> list[str]:
    """setInterval callbacks in this source that reach the network unguarded."""
    code = _strip_comments(source)
    defs, guarded = _definitions(code)
    bad = []
    for m in re.finditer(r"\bsetInterval\s*\(", code):
        args = _balanced(code, m.end() - 1)
        cb = _first_arg(args)
        if _reaches_network(cb, defs) and not _is_guarded(cb, defs, guarded):
            line = code.count("\n", 0, m.start()) + 1
            bad.append("line %d: setInterval(%s, ...)" % (line, cb[:60].replace("\n", " ")))
    return bad


# Ticks that reach the server only through a one-off step, not a repeating
# request, keyed by file and a piece of the callback.
NOT_A_POLL = {
    # The idle check opens the screensaver (which loads its photo list once)
    # and returns at once while the saver is up, so it never repeats a request.
    ("screensaver.js", "cameraOnScreen()"),
}


def _page_scripts() -> list[Path]:
    return sorted(p for p in JS.rglob("*.js")
                  if "vendor" not in p.parts and p != DOM_JS)


def test_every_repeating_poll_has_an_in_flight_guard():
    offenders = {}
    for path in _page_scripts():
        rel = str(path.relative_to(JS))
        bad = [b for b in unguarded_polls(path.read_text())
               if not any(rel == f and key in _callback_text(path, b) for f, key in NOT_A_POLL)]
        if bad:
            offenders[rel] = bad
    assert not offenders, (
        "wrap these setInterval polls in PR.poller (lib/dom.js) so a slow answer "
        "cannot stack requests and the request is dropped when the page is left: "
        + json.dumps(offenders, indent=1)
    )


@pytest.mark.parametrize("snippet", [
    "setInterval(poll, 3000); function poll() { fetch('timers'); }",
    "setInterval(() => { if (!document.hidden) load(); }, 5000);\n"
    "async function load() { const r = await fetch('x'); }",
    "var refresh = function () { img.src = 'snap?' + Date.now(); }; setInterval(refresh, 1500);",
    "setInterval(async function () { await PR.fetchJSON('x'); }, 1000);",
])
def test_the_sweep_catches_an_unguarded_poll(snippet):
    assert unguarded_polls(snippet), snippet


@pytest.mark.parametrize("snippet", [
    "var poll = PR.poller(function (signal) { return fetch('t', { signal: signal }); });"
    " setInterval(poll, 3000);",
    "setInterval(PR.poller(async (signal) => { await fetch('x', { signal }); }), 1500);",
    "let t = null; function poll() { if (!t) t = PR.poller(load); return t(); }\n"
    "async function load(signal) { await fetch('x', { signal }); } setInterval(poll, 5000);",
    "const tick = PR.poller(load); async function load(s) { await fetch('x'); }\n"
    "setInterval(() => { if (pane.active) tick(); }, 4000);",
    "setInterval(render, 1000); function render() { el.textContent = 'x'; }",
])
def test_the_sweep_accepts_a_guarded_or_local_tick(snippet):
    assert not unguarded_polls(snippet), snippet


# --- every page that uses PR.poller loads dom.js first ----------------------

def _script_srcs(html: str) -> list[str]:
    return re.findall(r'<script[^>]*\ssrc="([^"]+)"', html)


def _uses_poller(src: str) -> bool:
    m = re.search(r"static/js/([^?\"]+\.js)", src)
    if not m or "vendor/" in m.group(1):
        return False
    path = JS / m.group(1)
    return path.is_file() and "PR.poller(" in path.read_text()


@pytest.mark.parametrize("page", sorted(p.name for p in TEMPLATES.glob("*.html")))
def test_standalone_pages_load_dom_js_before_a_poller(page):
    html = (TEMPLATES / page).read_text()
    if "{% extends" in html or "<html" not in html:
        return  # base.html loads dom.js first for every page that extends it
    srcs = _script_srcs(html)
    users = [i for i, s in enumerate(srcs) if _uses_poller(s)]
    if not users:
        return
    dom = [i for i, s in enumerate(srcs) if "lib/dom.js" in s]
    assert dom and dom[0] < users[0], (page, srcs)


# --- PR.poller itself, in node -----------------------------------------------

needs_node = pytest.mark.skipif(NODE is None, reason="node is not available")


def _node(body: str):
    script = (
        "const listeners = {};\n"
        "globalThis.window = { addEventListener: (n, f) => { (listeners[n] = listeners[n] || []).push(f); } };\n"
        f"const PR = require({json.dumps(str(DOM_JS))});\n"
        "const fire = (n, ev) => (listeners[n] || []).forEach(f => f(ev || {}));\n"
        "const sleep = ms => new Promise(r => setTimeout(r, ms));\n"
        "(async () => {\n" + body + "\n})().then(() => process.exit(0))"
        ".catch(e => { console.error(e); process.exit(1); });\n"
    )
    out = subprocess.run([NODE, "-"], input=script, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_poller_skips_a_tick_while_the_last_is_running():
    res = _node(
        "let runs = 0; let release;\n"
        "const tick = PR.poller((signal) => { runs++; return new Promise(r => { release = r; }); });\n"
        "tick(); tick(); tick();\n"
        "const during = runs;\n"
        "release(); await sleep(5);\n"
        "tick(); await sleep(5);\n"
        "console.log(JSON.stringify({ during, after: runs }));"
    )
    assert res == {"during": 1, "after": 2}


@needs_node
def test_poller_gives_each_request_a_deadline_and_passes_arguments():
    res = _node(
        "let got = null;\n"
        "const tick = PR.poller((signal, a) => new Promise(r => {\n"
        "  signal.addEventListener('abort', () => { got = a; r(); });\n"
        "}), { timeout: 30 });\n"
        "await tick('x');\n"
        "console.log(JSON.stringify({ got }));"
    )
    assert res == {"got": "x"}


@needs_node
def test_poller_aborts_and_pauses_when_the_page_is_left():
    res = _node(
        "let aborted = false; let runs = 0;\n"
        "const tick = PR.poller((signal) => { runs++; return new Promise(r => {\n"
        "  signal.addEventListener('abort', () => { aborted = true; r(); });\n"
        "}); });\n"
        "tick();\n"
        "fire('pagehide'); await sleep(5);\n"
        "tick(); const whileGone = runs;\n"
        "fire('pageshow', { persisted: true }); tick(); await sleep(5);\n"
        "console.log(JSON.stringify({ aborted, whileGone, back: runs }));"
    )
    assert res == {"aborted": True, "whileGone": 1, "back": 2}


@needs_node
def test_poller_survives_a_poll_that_throws():
    res = _node(
        "let runs = 0;\n"
        "const tick = PR.poller(() => { runs++; throw new Error('boom'); });\n"
        "await tick(); await tick();\n"
        "console.log(JSON.stringify({ runs }));"
    )
    assert res == {"runs": 2}
