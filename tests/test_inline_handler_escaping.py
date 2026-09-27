"""Values the user controls must reach the browser as data, not as code.

Every page here used to splice a name, a bucket id or an entity id straight
into an inline on*= handler with single quotes around it. One apostrophe or
double quote in that value closed the handler, so the rest of the string landed
in the tag as live attributes (a working onmouseover, on a page any signed-in
account can plant a row on) and the button itself stopped working. The fix is
the same everywhere: emit the value as a data-* attribute, which Jinja escapes,
and read it back from el.dataset in a delegated listener.

These tests render the real pages with hostile values and assert the handler
attribute is gone and the data attribute carries the escaped value.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402

# A value that breaks out of both quote styles at once.
HOSTILE = 'Bob\'s "Best" Jam'


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "test-key", raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _handlers(html: str) -> list[str]:
    """Every inline event-handler attribute body on the page, both quote styles."""
    return ([m.group(1) for m in re.finditer(r'\son[a-z]+="([^"]*)"', html)]
            + [m.group(1) for m in re.finditer(r"\son[a-z]+='([^']*)'", html)])


def _every_handler_is_a_complete_statement(html: str) -> None:
    """A handler amputated at the value's first quote is the tell that the value
    escaped the attribute. Balanced parentheses prove it did not."""
    for body in _handlers(html):
        assert body.count("(") == body.count(")"), body


# --- Expiry defaults: any signed-in account can add a rule -------------------

def test_defaults_edit_button_carries_data_not_code(client):
    r = client.post("/ui/defaults/create", data={
        "category": "Preserves", "name_pattern": HOSTILE,
        "storage_type": "dry", "default_days": "365", "notes": HOSTILE,
    }, follow_redirects=False)
    assert r.status_code in (200, 302, 303)

    html = client.get("/ui/defaults").text
    assert 'onclick="openEdit(' not in html, "the rule text is still spliced into a handler"
    assert 'data-edit-rule' in html
    # The escaped value is present, the raw one never is.
    assert "Bob&#39;s &#34;Best&#34; Jam" in html
    assert HOSTILE not in html
    _every_handler_is_a_complete_statement(html)


# --- Inventory panels: bucket ids come from custom storage categories --------

def test_inventory_panel_and_move_buttons_carry_data_not_code(client, monkeypatch):
    monkeypatch.setattr(settings, "custom_storage_categories",
                        [{"label": "Cellar", "key": HOSTILE}], raising=False)
    html = client.get("/ui/inventory").text
    assert "togglePanel('" not in html
    assert "moveFromModal('" not in html
    assert "data-toggle-panel=" in html
    assert "data-move-to=" in html
    assert HOSTILE not in html
    _every_handler_is_a_complete_statement(html)


# --- Gadget lists: entity ids and device hosts are typed by the user ---------

def test_gadget_remove_buttons_carry_data_not_code(client, monkeypatch):
    monkeypatch.setattr(settings, "gadget_ha_entities", [HOSTILE], raising=False)
    monkeypatch.setattr(settings, "gadget_esp_devices",
                        [{"host": HOSTILE, "sensor": "probe", "name": "Grill"}],
                        raising=False)
    html = client.get("/setup").text
    assert "gadgetsHaRemove('" not in html
    assert "gadgetsEspRemove('" not in html
    assert "data-gadget-ha-remove=" in html
    assert "data-gadget-esp-remove=" in html
    assert HOSTILE not in html
    _every_handler_is_a_complete_statement(html)

    # The same list is re-rendered in the browser after an add or a remove, and
    # that copy built the identical handler. It loads as a separate file, so the
    # page assertions above cannot see it.
    js = (SERVICE / "app" / "static" / "js" / "setup" / "panes.js").read_text()
    assert "onclick=\"gadgetsHaRemove(" not in js
    assert "data-gadget-ha-remove=" in js


# --- Recipe pages: recipe and suggestion text is written in the browser -------
#
# The Recipes, Cook and Meal Plan pages build their handlers in JavaScript
# template literals, so the Jinja-side checks above cannot see them. A name
# run through escHtml or .replace(/'/g, '&#39;') is still unsafe there: the
# browser decodes &#39; back to ' before it runs the handler, so a suggestion
# named Stew');window.pwned=true;// ran on click. Values now reach a handler as
# an index or as an attribute-escaped JSON literal (jsArg).

_TEMPLATES = SERVICE / "app" / "templates"
_RECIPE_PAGES = ("cook.html", "recipes.html", "mealplan.html")
_QUOTED_INTERPOLATION = re.compile(r"""\son[a-z]+="[^"]*'\$\{""")
_HAND_ESCAPED = re.compile(r"""\son[a-z]+="[^"]*(?:escHtml\(|\.replace\(/'/g)""")


@pytest.mark.parametrize("page", _RECIPE_PAGES)
def test_recipe_page_handlers_do_not_quote_values_by_hand(page):
    src = (_TEMPLATES / page).read_text()
    bad = [f"{page}:{src.count(chr(10), 0, m.start()) + 1}: {m.group(0).strip()}"
           for rx in (_QUOTED_INTERPOLATION, _HAND_ESCAPED) for m in rx.finditer(src)]
    assert not bad, "a value is hand-quoted into an inline handler:\n" + "\n".join(bad)


def _escaping_helpers():
    spec = importlib.util.spec_from_file_location(
        "_recipe_output_escaping", Path(__file__).with_name("test_recipe_output_escaping.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Onclicks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.bodies: list[str] = []

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if k == "onclick":
                self.bodies.append(v)


def _run_handlers(html_by_label: dict, stubs: list[str], node: str) -> list:
    """Run every onclick body (as the browser would, after entity decoding) with
    the page functions stubbed to record their string arguments."""
    bodies = []
    for html in html_by_label.values():
        p = _Onclicks()
        p.feed(html)
        bodies.extend(p.bodies)
    script = (
        "const calls = [];\n"
        + "".join(f"globalThis.{n} = (...a) => calls.push([{json.dumps(n)}, ...a.filter(x => typeof x !== 'object')]);\n"
                  for n in stubs)
        + f"for (const b of {json.dumps(bodies)}) {{ (new Function(b)).call({{}}); }}\n"
        + "console.log(JSON.stringify({calls, pwned: globalThis.pwned === undefined ? null : true}));"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


_NODE_BIN = shutil.which("node")
_BREAKOUT = "Stew');window.pwned=true;//"
_ENTITY_BREAKOUT = '&quot;);window.pwned=true;//'


@pytest.mark.skipif(_NODE_BIN is None, reason="node is not available")
def test_cook_page_handlers_pass_names_as_data():
    h = _escaping_helpers()
    s = {"source": "mealie", "slug": _BREAKOUT, "id": _ENTITY_BREAKOUT, "name": _BREAKOUT,
         "matched_ingredients": [], "unmatched_ingredients": ["salt"], "staple_ingredients": [],
         "expiring_items_used": [], "total_ingredients": 1}
    rendered = h.render(
        "cook.html",
        ["escHtml", "safeImageUrl", "jsArg", "ownRow", "ingredientDetail", "actions",
         "stockCoverage", "srcBadge", "madeNote", "suggestionCard", "aiSuggestionCard",
         "openAiSuggestion"],
        'var LIBRARY_NAME = "Library"; var cardSeq = 0; var cookCards = {};',
        "{local: suggestionCard(%s, 'shopping'), web: suggestionCard(%s, 'shopping'),"
        " ai: aiSuggestionCard(%s, 0)}" % (
            json.dumps(s), json.dumps({**s, "source": "themealdb", "external_id": _BREAKOUT}),
            json.dumps({"name": _BREAKOUT})),
    )
    res = _run_handlers(rendered, ["openCookPreview", "cookThis", "addCourse", "openPlan",
                                   "cookedRecipe", "addMissing", "cookExternal",
                                   "importExternal", "openAiSuggestion"], _NODE_BIN)
    assert res["pwned"] is None, res
    calls = {c[0]: c[1:] for c in res["calls"]}
    assert calls["openPlan"] == [_ENTITY_BREAKOUT, _BREAKOUT]
    assert calls["cookThis"] == [_BREAKOUT]
    assert calls["cookExternal"][0] == _BREAKOUT
    assert calls["openAiSuggestion"] == [0]


@pytest.mark.skipif(_NODE_BIN is None, reason="node is not available")
def test_recipes_and_meal_plan_handlers_pass_names_as_data():
    h = _escaping_helpers()
    r = {"source": "mealie", "slug": _BREAKOUT, "name": _ENTITY_BREAKOUT}
    rendered = h.render(
        "recipes.html",
        ["esc", "safeImageUrl", "jsArg", "sourceBadge", "madeBadge", "recipeRow"],
        "var NATIVE_BACKEND = true;",
        "{mine: recipeRow(%s), web: recipeRow(%s)}" % (
            json.dumps(r), json.dumps({**r, "source": "forager", "external_id": _BREAKOUT})),
    )
    res = _run_handlers(rendered, ["openMealiePreview", "openRecipeEditor", "openShare",
                                   "setCurrentFromMealie", "openPreview", "saveExternal",
                                   "cookExternal"], _NODE_BIN)
    assert res["pwned"] is None, res
    calls = {c[0]: c[1:] for c in res["calls"]}
    assert calls["openMealiePreview"] == [_BREAKOUT, _ENTITY_BREAKOUT]
    assert calls["openPreview"] == [_BREAKOUT, "forager"]

    plan = h.render(
        "mealplan.html", ["escHtml", "jsArg", "planEntryRow", "recipeResultsHtml"],
        "var MEALIE_URL = null; var TYPE_BADGE = {};",
        "{row: planEntryRow(%s), search: recipeResultsHtml([%s])}" % (
            json.dumps({"id": 7, "entry_type": "dinner", "title": _BREAKOUT}),
            json.dumps({"id": _BREAKOUT, "name": _BREAKOUT})),
    )
    res = _run_handlers(plan, ["removeEntry", "pickRecipe"], _NODE_BIN)
    assert res["pwned"] is None, res
    calls = {c[0]: c[1:] for c in res["calls"]}
    assert calls["removeEntry"] == [7]
    assert calls["pickRecipe"][0] == _BREAKOUT


@pytest.mark.skipif(_NODE_BIN is None, reason="node is not available")
def test_cook_page_plan_button_passes_a_native_recipe_id_as_a_string():
    """Native library recipes have integer ids. The meal plan endpoint takes
    recipe_id as a string, so Plan must hand over "5", not 5, or saving the
    entry fails validation."""
    h = _escaping_helpers()
    s = {"source": "mealie", "slug": "stew", "id": 5, "name": "Stew",
         "matched_ingredients": [], "unmatched_ingredients": [], "staple_ingredients": [],
         "expiring_items_used": [], "total_ingredients": 0}
    rendered = h.render(
        "cook.html",
        ["escHtml", "safeImageUrl", "jsArg", "ownRow", "ingredientDetail", "actions",
         "stockCoverage", "srcBadge", "madeNote", "suggestionCard"],
        'var LIBRARY_NAME = "Library"; var cardSeq = 0; var cookCards = {};',
        "{local: suggestionCard(%s, 'ready')}" % json.dumps(s),
    )
    res = _run_handlers(rendered, ["openCookPreview", "cookThis", "addCourse", "openPlan",
                                   "cookedRecipe", "addMissing"], _NODE_BIN)
    calls = {c[0]: c[1:] for c in res["calls"]}
    assert calls["openPlan"] == ["5", "Stew"]
