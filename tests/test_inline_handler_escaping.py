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

import os
import re
import sys
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
