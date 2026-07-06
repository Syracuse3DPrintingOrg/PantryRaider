"""Degraded-mode behavior with an upstream service down (FoodAssistant-2cmm).

Simulates a dead Grocy / Mealie by replacing the shared httpx client with one
whose every request raises ConnectError, then checks that the API answers with
an honest, user-forward message (a 502 JSON detail or an error field) instead
of a raw 500, and that server-rendered pages keep their shell up with the
outage banner. See docs/design/degraded-modes.md for the full page audit.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import grocy as grocy_svc  # noqa: E402
from app.services import mealie as mealie_svc  # noqa: E402


class _DeadClient:
    """Stands in for the shared httpx.AsyncClient of a downed service."""

    async def request(self, *args, **kwargs):
        raise httpx.ConnectError("All connection attempts failed")


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie.test", raising=False)
    monkeypatch.setattr(settings, "mealie_api_key", "token", raising=False)
    # Skip the setup-redirect middleware: this install is "configured", its
    # backends are just down.
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True,
                        raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


@pytest.fixture
def grocy_down(monkeypatch):
    monkeypatch.setattr(grocy_svc, "_client", _DeadClient())


@pytest.fixture
def mealie_down(monkeypatch):
    monkeypatch.setattr(mealie_svc, "_client", _DeadClient())


# -- client-level wrap ---------------------------------------------------


@pytest.mark.anyio
async def test_grocy_connect_error_becomes_grocy_error(grocy_down):
    with pytest.raises(grocy_svc.GrocyError) as exc:
        await grocy_svc.GrocyClient().get_stock()
    assert "not reachable" in str(exc.value)


@pytest.mark.anyio
async def test_mealie_connect_error_becomes_mealie_error(mealie_down):
    with pytest.raises(mealie_svc.MealieError) as exc:
        await mealie_svc.MealieClient().get_shopping_lists()
    assert "not reachable" in str(exc.value)


def test_unreachable_message_names_main_server_on_satellite(monkeypatch):
    monkeypatch.setattr(type(settings), "is_satellite",
                        lambda self: True, raising=False)
    assert "main server" in grocy_svc.unreachable_message()
    assert "main server" in mealie_svc.unreachable_message()


# -- JSON endpoints answer 502 with honest copy, never a raw 500 ----------


@pytest.mark.parametrize("path", [
    "/inventory/dashboard",
    "/inventory/stock",
    "/expiring/",
    "/expiring/summary",
    "/audit/locations",
])
def test_grocy_endpoints_return_honest_502(client, grocy_down, path):
    r = client.get(path)
    assert r.status_code == 502
    assert "not reachable" in r.json()["detail"]


@pytest.mark.parametrize("path", [
    "/mealie/mealplan",
    "/mealie/recipes",
    "/mealie/suggest?external=false",
])
def test_mealie_endpoints_return_honest_502(client, mealie_down, path):
    r = client.get(path)
    assert r.status_code == 502
    assert "not reachable" in r.json()["detail"]


def test_shopping_list_degrades_with_error_field(client, mealie_down):
    # The Shopping page parses this JSON directly, so the outage arrives as an
    # error field on a 200 rather than an error status.
    r = client.get("/mealie/shopping")
    assert r.status_code == 200
    data = r.json()
    assert data["items"] == [] and data["list"] is None
    assert "not reachable" in data["error"]


def test_suggest_carries_grocy_outage_note(client, grocy_down):
    # Mealie unconfigured here would 400; monkeypatch a working recipe list so
    # only Grocy is down.
    async def fake_recipes(self):
        return [{"name": "Toast", "recipeIngredient": [{"note": "bread"}]}]
    with patch.object(mealie_svc.MealieClient, "get_recipes_with_ingredients",
                      fake_recipes):
        r = client.get("/mealie/suggest?external=false")
    assert r.status_code == 200
    data = r.json()
    assert "not reachable" in (data["grocy_error"] or "")
    assert data["inventory_items"] == 0


def test_expiring_display_text_stays_plain(client, grocy_down):
    r = client.get("/expiring/display")
    assert r.status_code == 200
    assert r.text == "Inventory unavailable"


# -- server-rendered pages keep their shell with a banner ------------------


def test_expiring_page_shows_outage_banner_not_all_clear(client, grocy_down):
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/ui/expiring")
    assert r.status_code == 200
    assert "not reachable" in r.text
    # The celebratory empty state must not show during an outage.
    assert "Nothing expiring within" not in r.text


def test_inventory_page_has_outage_banner_slot(client):
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/ui/inventory")
    assert r.status_code == 200
    assert 'id="inventory-status"' in r.text


# -- deck/HA summary endpoints keep their designed soft degradation --------


def test_shopping_count_degrades_to_zero(client, mealie_down):
    r = client.get("/mealie/shopping/count")
    assert r.status_code == 200
    assert r.json() == {"count": 0}


def test_mealplan_summary_reports_unreachable(client, mealie_down):
    r = client.get("/mealie/mealplan/summary")
    assert r.status_code == 200
    assert r.json().get("error") == "unreachable"
