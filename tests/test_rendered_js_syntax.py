"""Every rendered page's JavaScript still parses, on every page.

Two shipped bugs had the same shape: a value the user typed (a rule name, a
storage bucket key, an entity id) was spliced into JavaScript, and one
apostrophe or double quote in it closed the string early. The rest of the value
landed in the page as live markup, the control stopped working, and CI stayed
green because nothing parsed the JavaScript the templates actually produce.

So parse it. Render every UI page with hostile values planted wherever the user
controls the text, then run each inline script and each on*= handler through
node. A quote that escapes its string turns into a syntax error here instead of
a broken button in someone's kitchen.

Two things are deliberately out of scope:

* scripts loaded with src=, which are plain files already checked by the
  workflow's node --check pass, and
* script blocks that are not JavaScript (a type="application/json" data island),
  which node has no business parsing.

Skipped when node is not installed.
"""
from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

_NODE = shutil.which("node")

# One value that breaks out of both quote styles, plus a backslash and a
# closing tag, the other two ways a value escapes its context.
HOSTILE = 'Bob\'s "Best" Jam \\ </script>'

pytestmark = pytest.mark.skipif(_NODE is None, reason="node is not available")


# Settings this module plants hostile values into. The settings object is a
# process-wide singleton shared with every other test module, so each one is
# put back on the way out.
_PLANTED_SETTINGS = {
    "custom_storage_categories": [{"label": HOSTILE, "key": HOSTILE}],
    "gadget_ha_entities": [HOSTILE],
    "gadget_esp_devices": [{"host": HOSTILE, "sensor": "probe", "name": HOSTILE}],
    "streamdeck_cameras": [{"name": HOSTILE,
                            "stream_url": "http://cam.test/stream",
                            "snapshot_url": "http://cam.test/snap"}],
    "staple_items": HOSTILE,
    "grocy_base_url": "http://grocy.test",
    "grocy_api_key": "test-grocy-key",
    "vision_provider": "gemini",
    "gemini_api_key": "test-gemini-key",
    "auth_required": False,
    "auth_password": "",
}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    from app.config import settings

    saved = {k: getattr(settings, k) for k in (*_PLANTED_SETTINGS, "data_dir")}
    try:
        # data_dir must be set before app.main imports database.py, which
        # creates the directory at import time.
        settings.data_dir = str(tmp_path_factory.mktemp("data"))

        from app.main import app
        from fastapi.testclient import TestClient

        for key, value in _PLANTED_SETTINGS.items():
            setattr(settings, key, value)

        with TestClient(app) as c:
            planted = _plant_hostile_rows(c)
            try:
                yield c
            finally:
                _clear_hostile_rows(c, planted)
    finally:
        for key, value in saved.items():
            setattr(settings, key, value)
        os.chdir(cwd)


def _plant_hostile_rows(c) -> dict:
    """Rows a signed-in account can create, each named with the hostile value."""
    c.post("/ui/defaults/create", data={
        "category": HOSTILE, "name_pattern": HOSTILE, "storage_type": "dry",
        "default_days": "365", "notes": HOSTILE,
    }, follow_redirects=False)
    timer = c.post("/timers", json={"label": HOSTILE, "seconds": 600})
    c.post("/current-recipe", json={
        "title": HOSTILE, "source": "ai", "servings": 4,
        "ingredients": [{"name": HOSTILE, "quantity": 3, "unit": "ea"}],
        "steps": [HOSTILE],
    })
    recipe = c.post("/mealie/recipes/create", json={
        "name": HOSTILE, "ingredients": [HOSTILE], "instructions": [HOSTILE],
    })
    return {
        "timer_id": timer.json().get("timer", {}).get("id") if timer.status_code == 200 else None,
        "recipe_slug": recipe.json().get("slug") if recipe.status_code == 200 else None,
    }


def _clear_hostile_rows(c, planted: dict) -> None:
    """The app database and the state files outlive this module, so take the
    planted rows back out rather than leaving them for the next test."""
    from app.database import SessionLocal
    from app.models.db_models import ExpiryDefault

    c.delete("/current-recipe")
    if planted.get("timer_id"):
        c.delete(f"/timers/{planted['timer_id']}")
    if planted.get("recipe_slug"):
        c.delete(f"/recipes/{planted['recipe_slug']}")
    db = SessionLocal()
    try:
        db.query(ExpiryDefault).filter(
            ExpiryDefault.name_pattern == HOSTILE).delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _stock(monkeypatch):
    """Grocy is not reachable in tests, so hand the pages hostile stock rows."""
    from app.services.grocy import GrocyClient

    async def _full_stock(self):
        return [{"product_id": 1, "name": HOSTILE, "amount": 3.0,
                 "days_remaining": 2, "storage_bucket": "other",
                 "location": HOSTILE}]

    async def _expiring(self, days=7):
        return [{"product_id": 1, "amount": 2.0, "days_remaining": 1,
                 "best_before_date": "2026-07-04",
                 "product": {"name": HOSTILE}, "name": HOSTILE}]

    async def _stock_rows(self):
        return [{"product_id": 1, "amount": 3.0, "amount_opened": 1.0}]

    async def _products(self):
        return [{"id": 1, "name": HOSTILE}]

    monkeypatch.setattr(GrocyClient, "get_full_stock", _full_stock)
    monkeypatch.setattr(GrocyClient, "get_expiring", _expiring)
    monkeypatch.setattr(GrocyClient, "get_stock", _stock_rows)
    monkeypatch.setattr(GrocyClient, "get_products", _products)


def _ui_pages() -> list[str]:
    """Every parameterless GET page the app serves, from its own OpenAPI map, so
    a new page is covered the day it lands."""
    from app.main import app

    paths = app.openapi()["paths"]
    return sorted(p for p, ops in paths.items()
                  if "get" in ops and "{" not in p
                  and (p.startswith("/ui") or p == "/setup"))


_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)
_HANDLER_RE = re.compile(r"""\son[a-z]+\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_TYPE_RE = re.compile(r"""\btype\s*=\s*["']?([^"'\s>]+)""", re.I)
_JS_TYPES = {"text/javascript", "application/javascript", "module",
             "text/ecmascript", "application/ecmascript"}


def _snippets(page: str, body: str) -> list[tuple[str, str]]:
    """(label, JavaScript source) for every script and handler on the page."""
    out: list[tuple[str, str]] = []
    for i, m in enumerate(_SCRIPT_RE.finditer(body)):
        attrs, source = m.group(1), m.group(2).strip()
        if not source or "src=" in attrs.lower():
            continue
        kind = _TYPE_RE.search(attrs)
        if kind and kind.group(1).lower() not in _JS_TYPES:
            continue  # a data island, not code
        out.append((f"{page} inline script {i}", source))

    # Handlers only in the markup: an on*= inside a script body is a fragment of
    # a string the script builds, not a complete statement.
    markup = _SCRIPT_RE.sub("", body)
    for i, m in enumerate(_HANDLER_RE.finditer(markup)):
        attr = html.unescape(m.group(1) if m.group(1) is not None else m.group(2))
        if attr.strip():
            out.append((f"{page} handler {i}", attr))
    return out


def _node_check(tmp_path: Path, label: str, source: str, wrap: bool) -> str:
    """Empty string when it parses, the parser's complaint when it does not."""
    # A handler body is a function body, so it needs a function around it before
    # node will accept a bare `return` or `this`.
    text = f"function handler(event) {{\n{source}\n}}\n" if wrap else source
    f = tmp_path / (re.sub(r"[^a-z0-9]+", "_", label.lower()) + ".js")
    f.write_text(text)
    proc = subprocess.run([_NODE, "--check", str(f)],
                          capture_output=True, text=True, timeout=60)
    return "" if proc.returncode == 0 else proc.stderr.strip()


def test_every_rendered_page_has_parseable_javascript(client, tmp_path):
    pages = _ui_pages()
    assert len(pages) > 30, "the page list collapsed; check the OpenAPI walk"

    checked: dict[str, str] = {}   # source -> label it was first seen under
    rendered: list[str] = []
    failures: list[str] = []
    for page in pages:
        r = client.get(page, follow_redirects=False)
        if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
            continue  # a JSON or image endpoint under /ui, or a redirect
        rendered.append(page)
        for label, source in _snippets(page, r.text):
            if source in checked:
                continue  # the shared base template, already parsed
            checked[source] = label
            wrap = " handler " in label
            problem = _node_check(tmp_path, label, source, wrap)
            if problem:
                failures.append(f"{label}:\n{source[:400]}\n{problem}")

    assert failures == [], "\n\n".join(failures)
    # The two bugs this pins came from pages nobody was checking, so fail loudly
    # if the render loop quietly stops covering them.
    for page in ("/ui/inventory", "/ui/defaults", "/ui/timers", "/setup"):
        assert page in rendered, f"{page} never rendered, so it was never checked"
    assert len(rendered) > 20, f"only {len(rendered)} pages rendered as HTML"
    assert len(checked) > 100, f"only {len(checked)} snippets parsed"


def test_hostile_values_reach_the_pages(client):
    """The planting above has to actually land, or the parse test is checking
    pages that never saw a hostile value."""
    page = client.get("/ui/defaults").text
    assert "Bob&#39;s" in page or "Bob&#x27;s" in page
    assert HOSTILE not in page, "the hostile value reached the page unescaped"


def test_a_broken_handler_is_caught(tmp_path):
    """Pin the checker itself: the exact breakage that shipped twice."""
    broken = "openEdit('Bob's \"Best\" Jam')"
    assert _node_check(tmp_path, "sample broken", broken, wrap=True)
    assert _node_check(tmp_path, "sample ok", "openEdit(this.dataset.rule)",
                       wrap=True) == ""
