"""Every base.html page keeps its shared behaviour when browser storage is blocked.

base-page.js joined six inline scripts into one file. As separate tags, a throw
in one block only stopped that block; in one file a top-level throw stops
everything after it. A browser with site data blocked throws on any
localStorage access, and the kiosk toggle reads it at load, which used to take
the inbox badges, the health icon and both kiosk pollers down with it. The
file now wraps each block and reads storage through pageStorageGet().
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).resolve().parents[1] / "service" / "app" / "static" / "js"
          / "base-page.js")
_NODE = shutil.which("node")

_HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const listeners = {};
const stub = () => ({className: '', title: '', textContent: '', style: {removeProperty() {}},
                     classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
                     setAttribute() {}, addEventListener() {}});
const ctx = {
  console,
  setTimeout() { return 0; }, clearTimeout() {}, setInterval() { return 0; }, clearInterval() {},
  fetch: () => Promise.resolve({ok: true, json: () => Promise.resolve({count: 3})}),
  document: {
    addEventListener(name, fn) { (listeners[name] = listeners[name] || []).push(fn); },
    getElementById: stub, querySelector: () => null, querySelectorAll: () => [],
    body: stub(), documentElement: stub(),
  },
  location: {pathname: '/', href: '/', reload() {}},
  navigator: {userAgent: 'node'},
};
vm.createContext(ctx);
// Site data blocked: every storage access throws, as in Chromium, whether the
// page reads it as a bare global or through window.
vm.runInContext(`
  globalThis.window = globalThis;
  for (const name of ['localStorage', 'sessionStorage']) {
    Object.defineProperty(globalThis, name, {
      get() { throw new Error('SecurityError: Access is denied'); }});
  }`, ctx);
let loadError = null;
try { vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx); }
catch (e) { loadError = String(e); }
console.log(JSON.stringify({
  loadError,
  ready: (listeners['DOMContentLoaded'] || []).length,
  toggle: typeof ctx.toggleKioskMode,
  storage: typeof ctx.pageStorageGet === 'function' ? ctx.pageStorageGet('kioskMode') : 'missing',
}));
"""


@pytest.mark.skipif(_NODE is None, reason="node is not available")
def test_blocked_storage_does_not_stop_the_shared_page_behaviour(tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS)
    out = subprocess.run([_NODE, str(harness), str(SCRIPT)], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["loadError"] is None, got["loadError"]
    # The badges, the health indicator and both kiosk pollers each wait for
    # DOMContentLoaded; all four must still be registered.
    assert got["ready"] == 4, got
    # base.html's onclick calls this, so it must stay a top-level global.
    assert got["toggle"] == "function"
    # Blocked storage reads as empty rather than throwing.
    assert got["storage"] is None
