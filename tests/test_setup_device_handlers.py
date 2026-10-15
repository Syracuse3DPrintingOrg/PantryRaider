"""Device names and ids reach the Settings page as data, not as code.

Settings, Bandit Remotes lists Bandits and Bandit Cubs by the id each device
sends about itself (X-Device-Id on a Bandit's sync, X-Cub-Id on a Cub's poll),
and the Wi-Fi scan lists the network names nearby. Those values were spliced
into inline onclick and onchange handlers inside single quotes, after HTML
escaping: the browser decodes &#39; back into a quote before it runs the
handler, so an id like x');doSomething();// ran in the owner's session.

The data-on-* actions now take each value as a JSON string literal
(PR.escAttrJsStr). These tests render the real rows from
static/js/setup/hardware.js with hostile values, decode the action attributes
the way a browser does, run them through PR.runAction with the page functions
stubbed, and check that every function received the exact value and nothing
else ran.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
HARDWARE_JS = SERVICE / "app" / "static" / "js" / "setup" / "hardware.js"
DOM_JS = SERVICE / "app" / "static" / "js" / "lib" / "dom.js"
NODE = shutil.which("node")

BREAKOUTS = [
    "x');window.pwned=true;//",
    'x");window.pwned=true;//',
    "x&#39;);window.pwned=true;//",
    "x\\');window.pwned=true;//",
    "</script><img src=x onerror=window.pwned=true>",
]
_HANDLER_FUNCS = ["resyncDevice", "forgetDevice", "renameCub", "removeCub", "setCubView",
                  "setCubAutoUpdate", "pickSsid", "approvePairing", "denyPairing"]


def test_no_handler_quotes_a_value_by_hand():
    src = HARDWARE_JS.read_text()
    bad = re.findall(r"""on-?[a-z]+="[^"]*'\$\{""", src)
    assert not bad, bad
    assert ".replace(/'/g" not in src
    assert "PR.escAttrJsStr(" in src


def test_thermometer_and_sensor_rows_pass_names_as_data():
    """Settings, Thermometers & Sensors lists Bluetooth devices by the name
    each one broadcasts, which anyone nearby can choose."""
    panes = (SERVICE / "app" / "static" / "js" / "setup" / "panes.js").read_text()
    bad = [line.strip() for line in panes.splitlines()
           if re.search(r"""\s(?:data-on-|on)[a-z]+="[^"]*'\$\{PR\.esc\(""", line)]
    assert not bad, bad
    assert "PR.escAttrJsStr(dev.name" in panes


class _Handlers(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.bodies: list[str] = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name.startswith("data-on-") and value:
                self.bodies.append(value)


def _node(script: str):
    # The script goes in on stdin: a whole page script is too long for argv.
    out = subprocess.run([NODE, "-"], input=script, capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _render(values) -> list[str]:
    """Each row hardware.js builds, for every hostile value."""
    script = (
        "const vm = require('vm');\n"
        "const ctx = {document: {addEventListener() {}, getElementById() { return null; }},"
        " window: {}, console};\n"
        "vm.createContext(ctx);\n"
        f"vm.runInContext(require('fs').readFileSync({json.dumps(str(DOM_JS))}, 'utf8'), ctx);\n"
        f"vm.runInContext(require('fs').readFileSync({json.dumps(str(HARDWARE_JS))}, 'utf8'), ctx);\n"
        f"const values = {json.dumps(values)};\n"
        "const out = [];\n"
        "for (const v of values) {\n"
        "  out.push(ctx.deviceRowHtml({device_id: v, hostname: v, label: v, ip: '192.168.1.9',"
        " online: true}));\n"
        "  out.push(ctx.cubCardHtml({device_id: v, name: v, online: true,"
        " network_firmware: true, overrides: {}}));\n"
        "  out.push(ctx.wifiNetworkRowHtml({ssid: v, signal: 70, security: 'WPA2'}));\n"
        "  out.push(ctx.pairingRequestHtml({request_id: v, hostname: v, code: '1234'}));\n"
        "}\n"
        "console.log(JSON.stringify(out));\n"
    )
    return _node(script)


@pytest.mark.skipif(NODE is None, reason="node is not available")
def test_device_links_bracket_an_ipv6_address():
    script = (
        "const vm = require('vm');\n"
        "const ctx = {document: {addEventListener() {}, getElementById() { return null; }},"
        " window: {}, console};\n"
        "vm.createContext(ctx);\n"
        f"vm.runInContext(require('fs').readFileSync({json.dumps(str(DOM_JS))}, 'utf8'), ctx);\n"
        f"vm.runInContext(require('fs').readFileSync({json.dumps(str(HARDWARE_JS))}, 'utf8'), ctx);\n"
        "console.log(JSON.stringify([\n"
        "  ctx.deviceRowHtml({device_id: 'a', ip: 'fd00::12', deployment_mode: 'server'}),\n"
        "  ctx.deviceRowHtml({device_id: 'b', ip: 'fd00::13', deployment_mode: 'pi_remote'}),\n"
        "  ctx.deviceRowHtml({device_id: 'c', ip: '192.168.1.9', deployment_mode: 'server'}),\n"
        "]));\n"
    )
    v6, v6_remote, v4 = _node(script)
    assert 'href="http://[fd00::12]:9284/"' in v6 and ">fd00::12</a>" in v6
    assert 'href="http://[fd00::13]/"' in v6_remote
    assert 'href="http://192.168.1.9:9284/"' in v4


@pytest.mark.skipif(NODE is None, reason="node is not available")
def test_hostile_ids_reach_the_handlers_as_data():
    rows = _render(BREAKOUTS)
    bodies = []
    for row in rows:
        p = _Handlers()
        p.feed(row)
        bodies.extend(p.bodies)
    assert bodies, "no handlers were rendered"
    stubs = "".join(
        f"globalThis.{n} = (...a) => calls.push([{json.dumps(n)}, ...a.filter("
        "x => typeof x !== 'object')]);\n" for n in _HANDLER_FUNCS)
    script = (
        f"const PR = require({json.dumps(str(DOM_JS))});\n"
        "const calls = [];\nglobalThis.window = globalThis;\n" + stubs
        + "globalThis.pickPairingKind = () => {};\n"
        + f"for (const b of {json.dumps(bodies)}) {{\n"
        + "  try { PR.runAction(b, {dataset: {}, value: ''}, {}); }\n"
        + "  catch (e) { calls.push(['error', String(e)]); }\n"
        + "}\n"
        + "console.log(JSON.stringify({calls, pwned: globalThis.pwned === true}));\n"
    )
    res = _node(script)
    assert res["pwned"] is False, res
    errors = [c for c in res["calls"] if c[0] == "error"]
    assert not errors, errors
    seen = {}
    for call in res["calls"]:
        seen.setdefault(call[0], set()).update(a for a in call[1:] if isinstance(a, str))
    for name in ("resyncDevice", "forgetDevice", "renameCub", "removeCub", "setCubView",
                 "setCubAutoUpdate", "pickSsid", "approvePairing", "denyPairing"):
        assert seen.get(name) == set(BREAKOUTS), (name, seen.get(name))


@pytest.mark.skipif(NODE is None, reason="node is not available")
def test_sensor_cards_run_hostile_names_as_data():
    panes = SERVICE / "app" / "static" / "js" / "setup" / "panes.js"
    funcs = ["_stemmaDeviceCard", "_hygroDeviceCard", "_contactDeviceCard",
             "_buttonDeviceCard", "_gadgetsDeviceCard"]
    script = (
        "const vm = require('vm');\n"
        "const ctx = {document: {addEventListener() {}, getElementById() { return null; },"
        " querySelector() { return null; }, querySelectorAll() { return []; }},"
        " window: {}, console, localStorage: {getItem() { return null; }, setItem() {}}};\n"
        "vm.createContext(ctx);\n"
        f"vm.runInContext(require('fs').readFileSync({json.dumps(str(DOM_JS))}, 'utf8'), ctx);\n"
        f"vm.runInContext(require('fs').readFileSync({json.dumps(str(panes))}, 'utf8'), ctx);\n"
        f"const values = {json.dumps(BREAKOUTS)};\n"
        "const out = [];\n"
        "for (const v of values) {\n"
        "  const dev = {id: v, name: v, protocol: v, location: v, model: v, battery: 50,\n"
        "    mappings: {single: {action: 'shopping_add', product_name: v}},\n"
        "    keys: [{index: 0, label: v}], thresholds: {}, probes: [{index: 0, role: '', temp_c: 20}]};\n"
        f"  for (const f of {json.dumps(funcs)}) out.push(ctx[f](dev, 'c'));\n"
        "}\n"
        "console.log(JSON.stringify(out));\n"
    )
    rows = _node(script)
    bodies = []
    for row in rows:
        p = _Handlers()
        p.feed(row)
        bodies.extend(p.bodies)
    assert bodies
    names = sorted({m.group(1) for b in bodies for m in re.finditer(r"([A-Za-z_]\w*)\(", b)})
    stubs = "".join(
        f"globalThis.{n} = (...a) => calls.push([{json.dumps(n)}, ...a.filter("
        "x => typeof x === 'string')]);\n" for n in names if n not in ("this",))
    run = (
        f"const PR = require({json.dumps(str(DOM_JS))});\n"
        "const calls = [];\nglobalThis.window = globalThis;\n" + stubs
        + f"for (const b of {json.dumps(bodies)}) {{\n"
        + "  try { PR.runAction(b, {dataset: {}, value: '', checked: false}, {}); }\n"
        + "  catch (e) { calls.push(['error', String(e)]); }\n"
        + "}\n"
        + "console.log(JSON.stringify({calls, pwned: globalThis.pwned === true}));\n"
    )
    res = _node(run)
    assert res["pwned"] is False, res
    assert not [c for c in res["calls"] if c[0] == "error"], res["calls"][:5]
    # Every hostile value reached some handler intact.
    seen = {a for c in res["calls"] for a in c[1:]}
    assert set(BREAKOUTS) <= seen
