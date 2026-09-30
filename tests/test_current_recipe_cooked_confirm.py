"""On the Line asks before Cooked uses up pantry stock (FoodAssistant-hyr67).

Marking a recipe cooked consumes the matched items in Grocy, and there is no
undo. The Cooked button used to fire that POST on the first tap, which on a
kitchen touchscreen meant one stray touch removed real stock. The button now
opens a large in-page confirmation, and only its "Use them up" button sends
the request; Cancel closes it without touching anything.
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = (Path(__file__).resolve().parents[1] / "service" / "app" / "templates"
        / "current-recipe.html")
_NODE = shutil.which("node")


def _src() -> str:
    return PAGE.read_text()


def _extract(name: str) -> str:
    spec = importlib.util.spec_from_file_location(
        "_recipe_output_escaping", Path(__file__).with_name("test_recipe_output_escaping.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.extract_function(_src(), name)


def _cooked_button_handler() -> str:
    m = re.search(r'<button[^>]*onclick="([^"]*)"[^>]*>\s*<i class="bi bi-check2-circle me-1"></i>Cooked\s*</button>',
                  _src())
    assert m, "the Cooked button is missing"
    return m.group(1)


def test_cooked_button_only_opens_the_confirmation():
    handler = _cooked_button_handler()
    assert handler == "askCooked()", handler
    ask = _extract("askCooked")
    assert "fetch(" not in ask and "/cooked" not in ask, ask


def test_only_the_confirm_path_posts_cooked():
    src = _src()
    # The request to /cooked appears exactly once, inside confirmCooked.
    assert src.count("/cooked'") == 1, "the cooked request should live in one place"
    assert "/cooked'" in _extract("confirmCooked")
    for name in ("askCooked", "cancelCooked", "renderRecipe", "loadCurrentRecipe"):
        assert "/cooked'" not in _extract(name), name


def test_confirmation_has_big_confirm_and_cancel_buttons():
    src = _src()
    box = re.search(r'<div id="cr-cooked-confirm".*?</div>\s*</div>\s*</div>', src, re.S)
    assert box, "the confirmation block is missing"
    html = box.group(0)
    assert "d-none" in html.split(">", 1)[0], "the confirmation starts hidden"
    assert "This uses up the matching items in your pantry." in html
    assert re.search(r'btn-lg[^"]*"[^>]*onclick="confirmCooked\(this\)"', html)
    assert re.search(r'btn-lg[^"]*"[^>]*onclick="cancelCooked\(\)"', html)
    assert "Use them up" in html and "Cancel" in html
    assert "window.confirm" not in _extract("askCooked")


@pytest.mark.skipif(_NODE is None, reason="node is not available")
def test_cooked_flow_in_node():
    """Tap Cooked, then Cancel: no request. Tap Cooked, then Use them up: one
    POST to this slot's /cooked, and the page reloads as before."""
    functions = "\n".join(_extract(n) for n in ("askCooked", "cancelCooked", "confirmCooked"))
    script = """
const els = {};
function el(id) {
  if (!els[id]) {
    const cls = new Set(id === 'cr-cooked-confirm' ? ['d-none'] : []);
    els[id] = {id, disabled: false,
      classList: {add: c => cls.add(c), remove: c => cls.delete(c), contains: c => cls.has(c)},
      scrollIntoView() {}, focus() { globalThis.focused = id; }};
  }
  return els[id];
}
globalThis.document = {getElementById: el};
const requests = [];
let reloads = 0;
globalThis.fetch = async (url, opts) => { requests.push([url, opts && opts.method]);
  return {ok: true, json: async () => ({consumed: ['a', 'b']})}; };
globalThis.alert = () => {};
globalThis.prErrText = async () => 'x';
globalThis.prReason = () => 'x';
globalThis.loadCurrentRecipe = () => { reloads++; };
var crFocus = 2;
%s
(async () => {
  const box = el('cr-cooked-confirm');
  %s;                                   // the Cooked button
  const shownAfterTap = !box.classList.contains('d-none');
  const focusedAfterTap = globalThis.focused;
  cancelCooked();
  const afterCancel = {requests: requests.length, hidden: box.classList.contains('d-none')};
  %s;
  await confirmCooked(el('cr-cooked-yes'));
  console.log(JSON.stringify({shownAfterTap, focusedAfterTap, afterCancel, requests, reloads,
    hiddenAfterConfirm: box.classList.contains('d-none')}));
})();
""" % (functions, _cooked_button_handler(), _cooked_button_handler())
    out = subprocess.run([_NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    assert res["shownAfterTap"] is True
    assert res["focusedAfterTap"] == "cr-cooked-no", "Cancel takes focus, not the destructive button"
    assert res["afterCancel"] == {"requests": 0, "hidden": True}
    assert res["requests"] == [["current-recipe/2/cooked", "POST"]]
    assert res["reloads"] == 1
    assert res["hiddenAfterConfirm"] is True
