"""/setup/save and custom nav tabs whose address is not allowed.

When any posted tab's address could run code, none of the posted tabs are
saved (the stored ones stay put), every other field in the same save still
lands, and the reply is a 200 that names the tabs to fix under
errors.custom_nav_tabs. A save with only good tabs answers exactly as before,
so an older settings page that knows nothing about errors keeps working.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402

_STORED = [{"id": "custom_ha", "label": "HA", "icon": "bi-house",
            "url": "https://ha.example", "parent": "", "heading": False}]


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app

    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "custom_nav_tabs", list(_STORED), raising=False)
    monkeypatch.setattr(settings, "nav_order", "", raising=False)
    monkeypatch.setattr(settings, "nav_hidden", "", raising=False)
    monkeypatch.setattr(settings, "nav_parents", {}, raising=False)
    try:
        with patch.object(type(settings), "is_configured", lambda self: True):
            yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_rejected_tab_keeps_stored_tabs_and_saves_other_fields(client):
    r = client.post("/setup/save", json={
        "nav_order": "inventory,custom_ha,custom_evil",
        "custom_nav_tabs": [
            {"id": "custom_ha", "label": "HA", "icon": "bi-house", "url": "https://ha.example"},
            {"id": "custom_evil", "label": "Evil", "icon": "bi-bug",
             "url": "java\tscript:alert(1)"},
        ],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["errors"]["custom_nav_tabs"] == [{
        "id": "custom_evil", "label": "Evil",
        "message": ("Evil can't be saved: a tab's address has to start with "
                    "http:// or https://, or be a page in this app."),
    }]
    # The stored tabs are untouched; the rest of the save landed.
    assert settings.custom_nav_tabs == _STORED
    assert settings.nav_order == "inventory,custom_ha,custom_evil"


def test_good_tabs_save_as_before_with_no_errors_key(client):
    r = client.post("/setup/save", json={"custom_nav_tabs": [
        {"id": "custom_ha", "label": "HA", "icon": "bi-house", "url": "https://ha.example"},
        {"id": "custom_docs", "label": "Docs", "url": "ui/about"},
        {"id": "custom_tools", "label": "Tools", "heading": True},
    ]})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert [t["id"] for t in settings.custom_nav_tabs] == [
        "custom_ha", "custom_docs", "custom_tools"]
    assert settings.custom_nav_tabs[1]["url"] == "ui/about"
    assert settings.custom_nav_tabs[2]["heading"] is True


def test_empty_list_still_clears_and_null_still_keeps(client):
    assert client.post("/setup/save", json={"custom_nav_tabs": None}).json() == {"ok": True}
    assert settings.custom_nav_tabs == _STORED
    assert client.post("/setup/save", json={"custom_nav_tabs": []}).json() == {"ok": True}
    assert settings.custom_nav_tabs == []


def test_save_without_nav_fields_is_unchanged(client):
    # Every other pane's save posts no nav fields and gets the old reply.
    r = client.post("/setup/save", json={"nav_hidden": "shop"})
    assert r.json() == {"ok": True}
    assert settings.custom_nav_tabs == _STORED
