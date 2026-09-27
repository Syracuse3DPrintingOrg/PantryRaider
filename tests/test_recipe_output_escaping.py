"""Recipe text reaches the Recipes, Cook and Meal Plan pages as text, never markup.

Recipes come from places the owner does not control: Forager community
recipes from any account, file imports, AI drafts, TheMealDB and Spoonacular.
These pages build their cards with innerHTML template literals, so a recipe
named <img src=x onerror=...> ran as script in the owner's session and on the
kiosk. Every recipe field now goes through the page's escape helper, and image
URLs are limited to relative paths and http(s).

Two layers of checks:

* a static scan of the three templates for a recipe field spliced raw into a
  template literal, and
* a behavioural check that runs the real template functions in node with
  hostile recipes and parses the markup they return (skipped without node).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[1] / "service" / "app" / "templates"
PAGES = ("recipes.html", "cook.html", "mealplan.html")
_NODE = shutil.which("node")

# The escape helpers each page defines. An interpolation wrapped in one of
# these (or in a helper built on them) is safe in text and attributes.
SAFE_WRAPPERS = ("esc(", "escHtml(", "jsArg(", "Number(", "encodeURIComponent(")


def _source(name: str) -> str:
    return (TEMPLATES / name).read_text()


# --- static scan -----------------------------------------------------------

# A recipe-derived field spliced straight into a template literal.
RAW_FIELD = re.compile(
    r"\$\{\s*(?:r|s|d|e|it|s2)\.(?:name|description|attribution|image|title|"
    r"message|total_time|slug|external_id|source|id|recipe_slug|mealie_url|"
    r"grocy_error)\s*\}"
)
RAW_JOIN = re.compile(r"\$\{\s*(?:s|d)\.[a-z_]+\.join\(")
RAW_LIST_ITEM = re.compile(r"<li[^>]*>\$\{(?!\s*(?:esc|escHtml)\()")
RAW_ERROR = re.compile(r"\$\{\s*e\.message\s*\}")


# Sinks that never parse markup: a plain-text node, a URL handed to
# window.open, or an href set as a property.
_TEXT_SINKS = ("textContent", "window.open(", ".href =")


def _plain_text_context(lines: list[str], idx: int) -> bool:
    """A line with no markup of its own that feeds a non-HTML sink (on the same
    line or as the continuation of the statement above it)."""
    if "<" in lines[idx]:
        return False
    near = "".join(lines[max(0, idx - 2):idx + 1])
    return any(sink in near for sink in _TEXT_SINKS)


@pytest.mark.parametrize("page", PAGES)
def test_no_recipe_field_is_spliced_raw(page):
    src = _source(page)
    lines = src.split("\n")
    problems = []
    for rx in (RAW_FIELD, RAW_JOIN, RAW_LIST_ITEM, RAW_ERROR):
        for m in rx.finditer(src):
            idx = src.count("\n", 0, m.start())
            if _plain_text_context(lines, idx):
                continue
            problems.append(f"{page}:{idx + 1}: {m.group(0)}")
    assert not problems, "unescaped recipe text in markup:\n" + "\n".join(problems)


@pytest.mark.parametrize("page", PAGES)
def test_escape_helper_covers_quotes_and_non_strings(page):
    """The helper must escape quotes (it is used inside attributes) and cope
    with numbers and null, not just strings."""
    src = _source(page)
    name = "esc" if page == "recipes.html" else "escHtml"
    body = extract_function(src, name)
    assert "&quot;" in body and "&#39;" in body, body
    assert "String(s ?? '')" in body, body
    assert "createElement" not in body, "a DOM-based helper leaves quotes raw"


def test_image_urls_are_scheme_checked():
    for page in ("recipes.html", "cook.html"):
        src = _source(page)
        assert "function safeImageUrl(" in src, page
        assert not re.search(r'src="\$\{(?:r|s|d)\.image\}"', src), page
        # Every img src built in markup goes through the checked, escaped value.
        for m in re.finditer(r'<img src="\$\{([^}]*)\}"', src):
            assert m.group(1).startswith(("esc(", "escHtml(")), (page, m.group(0))


# --- a small JS function extractor -------------------------------------------

_REGEX_PREV = set("(,=:[!&|?{};+-*%<>~^")
_REGEX_WORDS = {"return", "typeof", "case", "in", "of"}


def _skip_string(src: str, i: int, quote: str) -> int:
    i += 1
    while i < len(src) and src[i] != quote:
        if src[i] == "\\":
            i += 1
        i += 1
    return i + 1


def _skip_regex(src: str, i: int) -> int:
    i += 1
    in_class = False
    while i < len(src):
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            break
        i += 1
    i += 1
    while i < len(src) and src[i].isalpha():
        i += 1
    return i


def _match_block(src: str, open_at: int) -> int:
    """Index just past the brace that closes the block opened at open_at.
    Tracks strings, comments, regex literals and nested template literals."""
    stack: list[dict] = [{"kind": "code", "depth": 0, "in_tpl": False}]
    i = open_at
    prev = "("
    n = len(src)
    while i < n:
        top = stack[-1]
        c = src[i]
        if top["kind"] == "tpl":
            if c == "\\":
                i += 2
                continue
            if c == "`":
                stack.pop()
                i += 1
                prev = "x"
                continue
            if c == "$" and src[i + 1:i + 2] == "{":
                stack.append({"kind": "code", "depth": 0, "in_tpl": True})
                i += 2
                prev = "("
                continue
            i += 1
            continue
        if src.startswith("//", i):
            i = src.index("\n", i)
            continue
        if src.startswith("/*", i):
            i = src.index("*/", i) + 2
            continue
        if c in "\"'":
            i = _skip_string(src, i, c)
            prev = "x"
            continue
        if c == "`":
            stack.append({"kind": "tpl"})
            i += 1
            continue
        if c == "/" and prev in _REGEX_PREV:
            i = _skip_regex(src, i)
            prev = "x"
            continue
        if c == "{":
            top["depth"] += 1
        elif c == "}":
            if top["depth"] == 0 and top["in_tpl"]:
                stack.pop()
                i += 1
                prev = "x"
                continue
            top["depth"] -= 1
            if len(stack) == 1 and top["depth"] == 0:
                return i + 1
        if not c.isspace():
            m = re.match(r"[A-Za-z0-9_$]+", src[i:])
            if m:
                prev = "(" if m.group(0) in _REGEX_WORDS else "x"
                i += len(m.group(0))
                continue
            prev = "x" if c in ")]" else c
        i += 1
    raise ValueError("unbalanced block")


def extract_function(src: str, name: str) -> str:
    m = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\(", src)
    assert m, f"function {name} not found"
    open_at = src.index("{", m.end())
    return src[m.start():_match_block(src, open_at)]


def run_node(script: str) -> object:
    out = subprocess.run([_NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def render(page: str, functions: list[str], prelude: str, calls: str) -> dict:
    """Load the named functions from a page and return {label: html} for each
    expression in `calls` (a JS object literal of label: expression)."""
    src = _source(page)
    body = "\n".join(extract_function(src, f) for f in functions)
    script = f"{prelude}\n{body}\nconsole.log(JSON.stringify({calls}));"
    return run_node(script)


# --- behavioural check -------------------------------------------------------

EVIL = "<img src=x onerror=window.pwned=1>"
EVIL_ATTR = 'x" onerror="window.pwned=1'


class _Markup(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


def _parse(html: str) -> _Markup:
    p = _Markup()
    p.feed(html)
    p.close()
    return p


def _assert_inert(html: str) -> _Markup:
    doc = _parse(html)
    for tag, attrs in doc.tags:
        # The hostile markup never becomes a tag of its own.
        assert tag != "svg", html
        if tag == "img":
            assert attrs.get("src") not in ("x",), html
            assert not str(attrs.get("src", "")).lower().startswith(("javascript:", "data:")), html
        for k, v in attrs.items():
            if k.startswith("on") and k != "onclick":
                assert v == "this.remove()", (k, v, html)
    return doc


needs_node = pytest.mark.skipif(_NODE is None, reason="node is not available")


@needs_node
def test_recipes_page_renders_hostile_recipes_as_text():
    hostile = {
        "source": "forager", "external_id": "1", "name": EVIL,
        "description": "<svg onload=window.pwned=1>", "attribution": EVIL,
        "image": EVIL_ATTR, "rating": 4, "rating_count": "<b>9</b>",
        "total_time": EVIL,
        "badge": {"css_class": 'x" onmouseover="window.pwned=1', "label": EVIL},
    }
    out = render(
        "recipes.html",
        ["esc", "safeImageUrl", "jsArg", "sourceBadge", "madeBadge", "recipeRow",
         "ingredientListHtml"],
        "var NATIVE_BACKEND = true;",
        "{forager: recipeRow(%s), mealie: recipeRow(%s), js: recipeRow(%s),"
        " ings: ingredientListHtml([%s], [%s])}" % (
            json.dumps(hostile),
            json.dumps({**hostile, "source": "mealie", "slug": "stew"}),
            json.dumps({**hostile, "image": "javascript:window.pwned=1"}),
            json.dumps(EVIL), json.dumps(EVIL)),
    )
    for label, html in out.items():
        doc = _assert_inert(html)
        assert EVIL in "".join(doc.text), (label, html)
    # A relative app image path still renders.
    ok = render("recipes.html", ["esc", "safeImageUrl", "jsArg", "sourceBadge", "madeBadge", "recipeRow"],
                "var NATIVE_BACKEND = true;",
                "{row: recipeRow(%s)}" % json.dumps({"source": "forager", "external_id": "1",
                                                     "name": "Soup", "image": "recipes/soup/image?v=1&x=2"}))
    imgs = [a for t, a in _parse(ok["row"]).tags if t == "img"]
    assert imgs and imgs[0]["src"] == "recipes/soup/image?v=1&x=2"


@needs_node
def test_cook_page_renders_hostile_suggestions_as_text():
    s = {
        "source": "mealie", "slug": "stew", "id": "1", "name": EVIL,
        "description": EVIL, "image": EVIL_ATTR, "expiring_items_used": [EVIL],
        "matched_ingredients": [EVIL], "unmatched_ingredients": [EVIL],
        "staple_ingredients": [EVIL], "total_ingredients": 3,
        "badge": {"css_class": "x", "label": EVIL},
        "last_cooked_at": '2026-01-01" onmouseover="x',
        "cook_count": 2,
    }
    out = render(
        "cook.html",
        ["escHtml", "safeImageUrl", "jsArg", "ownRow", "ingredientDetail", "actions",
         "stockCoverage", "srcBadge", "madeNote", "suggestionCard", "aiSuggestionCard"],
        'var LIBRARY_NAME = "Library"; var cardSeq = 0; var cookCards = {};',
        "{local: suggestionCard(%s, 'shopping'), web: suggestionCard(%s, 'shopping'),"
        " ai: aiSuggestionCard(%s, 0)}" % (
            json.dumps(s), json.dumps({**s, "source": "themealdb", "external_id": "9"}),
            json.dumps({"name": EVIL, "description": EVIL, "uses": [EVIL]})),
    )
    for label, html in out.items():
        doc = _assert_inert(html)
        assert EVIL in "".join(doc.text), (label, html)


@needs_node
def test_meal_plan_renders_hostile_titles_as_text():
    out = render(
        "mealplan.html",
        ["escHtml", "jsArg", "planEntryRow", "recipeResultsHtml"],
        "var MEALIE_URL = 'http://mealie.test'; var TYPE_BADGE = {dinner: 'primary'};",
        "{linked: planEntryRow(%s), plain: planEntryRow(%s), search: recipeResultsHtml([%s])}" % (
            json.dumps({"id": 3, "entry_type": "dinner", "title": EVIL, "recipe_slug": "stew"}),
            json.dumps({"id": 4, "entry_type": "dinner", "title": EVIL}),
            json.dumps({"id": "1", "name": EVIL})),
    )
    for label, html in out.items():
        doc = _assert_inert(html)
        assert EVIL in "".join(doc.text), (label, html)


@pytest.mark.parametrize("page", ("recipes.html", "cook.html"))
def test_source_links_are_scheme_checked(page):
    """A recipe's source URL becomes a clickable link, so it must be http(s)."""
    src = _source(page)
    assert not re.search(r"\.href = d\.source_url", src), page
    assert "safeLinkUrl(d.source_url)" in src, page


@needs_node
def test_safe_link_url_allows_only_http_links():
    urls = ["https://example.com/stew", "HTTP://x.test", "javascript:alert(1)",
            "java\tscript:alert(1)", " javascript:alert(1)", "data:text/html,x",
            "//evil.test/x", "recipes/1", None]
    for page in ("recipes.html", "cook.html"):
        out = render(page, ["safeLinkUrl"], "",
                     "{r: %s.map(safeLinkUrl)}" % json.dumps(urls))
        assert out["r"] == ["https://example.com/stew", "HTTP://x.test",
                            "", "", "", "", "", "", ""], (page, out)
