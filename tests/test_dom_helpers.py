"""The shared escape and fetch helpers in static/js/lib/dom.js.

Every page used to carry its own escape function, and they disagreed: some
left quotes alone, one dropped characters instead of escaping them. They all
go through PR.esc now, so this checks it once, in node, along with the
handler-argument helpers and fetchJSON. The page checks make sure every page
that calls PR.* actually loads the file, ahead of the scripts that use it.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "service" / "app"
DOM_JS = APP / "static" / "js" / "lib" / "dom.js"
TEMPLATES = APP / "templates"
NODE = shutil.which("node")

needs_node = pytest.mark.skipif(NODE is None, reason="node is not available")


def _node(body: str):
    script = (
        f"const PR = require({json.dumps(str(DOM_JS))});\n"
        "(async () => {\n" + body + "\n})().catch(e => { console.error(e); process.exit(1); });\n"
    )
    out = subprocess.run([NODE, "-"], input=script, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_esc_escapes_every_markup_character():
    res = _node("console.log(JSON.stringify([PR.esc(`<a href=\"x\" title='y'>&\\`</a>`),"
                " PR.esc(null), PR.esc(undefined), PR.esc(0), PR.esc(12.5), PR.esc(false)]));")
    assert res == [
        "&lt;a href=&quot;x&quot; title=&#39;y&#39;&gt;&amp;&#96;&lt;/a&gt;",
        "", "", "0", "12.5", "false",
    ]


@needs_node
def test_esc_attr_js_round_trips_through_a_handler():
    """A value written with escAttrJs into data-on-click="f(...)" reaches f intact,
    after the browser decodes the attribute, and never runs as code."""
    hostile = ["x');globalThis.pwned=true;//", 'x");globalThis.pwned=true;//',
               "&#39;);globalThis.pwned=true;//", "</script><img onerror=1>", "a`b${c}"]
    res = _node(
        f"const vals = {json.dumps(hostile)};\n"
        "const decode = s => s.replace(/&quot;/g, '\"').replace(/&#39;/g, \"'\")"
        ".replace(/&#96;/g, '`').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');\n"
        "const got = [];\nglobalThis.f = v => got.push(v);\n"
        "for (const v of vals) {\n"
        "  const attr = PR.escAttrJs(v);\n"
        "  if (/[\"'<>`]/.test(attr)) throw new Error('raw character left in ' + attr);\n"
        "  PR.runAction('f(' + decode(attr) + ')', {}, {});\n"
        "  PR.runAction('f(' + decode(PR.escAttrJsStr(v)) + ')', {}, {});\n"
        "}\n"
        "console.log(JSON.stringify({got, pwned: globalThis.pwned === true,"
        " num: PR.escAttrJs(7), numStr: PR.escAttrJsStr(7), nil: PR.escAttrJs(null)}));"
    )
    assert res["pwned"] is False
    assert res["got"] == [v for v in hostile for _ in (0, 1)]
    assert res["num"] == "7"
    assert res["numStr"] == "&quot;7&quot;"
    assert res["nil"] == "&quot;&quot;"


@needs_node
def test_fetch_json_sends_json_and_reports_the_server_reason():
    res = _node(
        "const seen = [];\n"
        "globalThis.FormData = class {}; globalThis.Blob = class {};\n"
        "globalThis.fetch = async (url, o) => {\n"
        "  seen.push({url, method: o.method || 'GET', body: o.body ?? null,"
        " ct: o.headers.get('Content-Type'), cred: o.credentials});\n"
        "  if (url === 'bad') return new Response(JSON.stringify({detail: 'Nope.'}), {status: 400});\n"
        "  if (url === 'html') return new Response('<h1>oops</h1>', {status: 502});\n"
        "  if (url === 'empty') return new Response('', {status: 200});\n"
        "  return new Response(JSON.stringify({ok: true}), {status: 200});\n"
        "};\n"
        "const out = {};\n"
        "out.ok = await PR.fetchJSON('things', {method: 'POST', body: {a: 1}});\n"
        "out.empty = await PR.fetchJSON('empty');\n"
        "for (const u of ['bad', 'html']) {\n"
        "  try { await PR.fetchJSON(u); } catch (e) { out[u] = [e.message, e.status]; }\n"
        "}\n"
        "out.seen = seen;\n"
        "console.log(JSON.stringify(out));"
    )
    assert res["ok"] == {"ok": True}
    assert res["empty"] is None
    assert res["bad"] == ["Nope.", 400]
    assert res["html"] == ["The server returned an error (HTTP 502).", 502]
    first = res["seen"][0]
    assert first == {"url": "things", "method": "POST", "body": '{"a":1}',
                     "ct": "application/json", "cred": "same-origin"}


def _script_srcs(html: str) -> list[str]:
    return re.findall(r'<script[^>]*\ssrc="([^"]+)"', html)


def test_base_loads_dom_js_before_every_other_script():
    srcs = _script_srcs((TEMPLATES / "base.html").read_text())
    assert srcs[0].startswith("static/js/lib/dom.js"), srcs[:3]
    tag = re.search(r'<script[^>]*src="static/js/lib/dom\.js[^"]*"[^>]*>',
                    (TEMPLATES / "base.html").read_text()).group(0)
    assert "defer" not in tag and "async" not in tag, tag


@pytest.mark.parametrize("page", ["setup.html", "start.html"])
def test_standalone_pages_that_use_pr_load_dom_js(page):
    html = (TEMPLATES / page).read_text()
    srcs = _script_srcs(html)
    dom = [i for i, s in enumerate(srcs) if s.startswith("static/js/lib/dom.js")]
    assert dom, page
    users = [i for i, s in enumerate(srcs) if "/setup/" in s or "pages/" in s]
    assert all(i > dom[0] for i in users), (page, srcs)


def test_no_page_defines_its_own_escape_helper():
    """One escape helper: a page-local copy is how the divergent ones crept in."""
    rx = re.compile(r"function\s+(esc|escHtml|escapeHtml|_gEsc|navEsc|_paneEsc|jsArg|_gJs)\s*\(")
    found = []
    for path in list((APP / "static" / "js").rglob("*.js")) + list(TEMPLATES.rglob("*.html")):
        if "vendor" in path.parts or path == DOM_JS:
            continue
        for m in rx.finditer(path.read_text()):
            found.append(f"{path.relative_to(APP)}: {m.group(0)}")
    assert not found, found


# --- data-on-* actions --------------------------------------------------------


@needs_node
def test_actions_call_page_functions_with_this_event_and_literals():
    action = ("save(this.value, 'a\\'b', \"c\\\"d\", -2.5, [1, {\"k\": null}],"
              " this.dataset.rid, !this.checked, event.key)")
    res = _node(
        "const got = [];\n"
        "globalThis.save = (...a) => { got.push(a); return 'saved'; };\n"
        "globalThis.NS = {deep: {fn: (v) => { got.push(['ns', v]); return false; }}};\n"
        "const el = {value: 'v', checked: true, dataset: {rid: '9'},\n"
        "  closest() { return {remove() { got.push(['removed']); }}; }};\n"
        "const ev = {key: 'Enter', prevented: false, preventDefault() { this.prevented = true; }};\n"
        f"const r1 = PR.runAction({json.dumps(action)}, el, ev);\n"
        "const r2 = PR.runAction(\"event.preventDefault(); this.closest('.row').remove();"
        " return NS.deep.fn(3)\", el, ev);\n"
        "console.log(JSON.stringify({got, r1: r1 === undefined ? 'undef' : r1, r2,"
        " prevented: ev.prevented}));"
    )
    assert res["got"][0] == ["v", "a'b", 'c"d', -2.5, [1, {"k": None}], "9", False, "Enter"]
    assert res["got"][1] == ["removed"]
    assert res["got"][2] == ["ns", 3]
    assert res["r1"] == "undef"          # no return: the result is not used
    assert res["r2"] is False            # return false cancels the event
    assert res["prevented"] is True


@needs_node
def test_actions_refuse_anything_but_calls_to_page_functions():
    bad = [
        "x = 1",                                   # assignment
        "save(1) + 1",                             # operators
        "save(window)",                            # a global as a value
        "save(save)",
        "this.ownerDocument.defaultView.save(1)",  # walking out of the element
        "event.view.save(1)",
        "constructor('x')",
        "save().then(1)",                          # chaining on a page result
        "document.getElementById('a').click()",
        "save(1) save(2)",
        "return save(1); save(2)",
        "function () {}",
        "`x`",
    ]
    res = _node(
        "globalThis.save = () => 1;\n"
        f"const bad = {json.dumps(bad)};\n"
        "const out = bad.map(b => { try { PR.parseAction(b); return 'parsed'; }"
        " catch (e) { return 'refused'; } });\n"
        "let native = 'ran';\n"
        "try { PR.runAction('parseInt(\"1\")', {}, {}); } catch (e) { native = 'refused'; }\n"
        "let nativeMember = 'ran';\n"
        "try { PR.runAction('JSON.stringify(1)', {}, {}); } catch (e) { nativeMember = 'refused'; }\n"
        "globalThis.confirm = () => true;\n"
        "const confirmOk = PR.runAction(\"return confirm('Sure?')\", {}, {});\n"
        "console.log(JSON.stringify({out, native, nativeMember, confirmOk}));"
    )
    assert res["out"] == ["refused"] * len(bad), list(zip(bad, res["out"]))
    assert res["native"] == "refused"        # built-in functions are not page functions
    assert res["nativeMember"] == "refused"
    assert res["confirmOk"] is True


@needs_node
def test_actions_resolve_registered_names_first():
    res = _node(
        "let got = null;\n"
        "PR.on('privateFn', v => { got = v; });\n"
        "PR.runAction('privateFn(7)', {}, {});\n"
        "console.log(JSON.stringify({got}));"
    )
    assert res["got"] == 7
