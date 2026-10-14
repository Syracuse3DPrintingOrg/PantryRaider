"""AI recipe suggestions are validated on the server before they reach the page.

The model's reply is untrusted text. The Cook page escapes what it renders, and
as a second line of defense suggest_llm now reduces every card to
{name, description, uses} with bounded lengths, dropping anything that is not a
card with a usable name.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SERVICE_DIR = Path(__file__).parent.parent / "service"


def test_clean_suggestions_bounds_and_drops_bad_entries():
    from app.routers.mealie import clean_suggestions

    raw = [
        {"name": "Tomato Soup", "description": "Uses the ripe tomatoes.",
         "uses": ["tomatoes", "onion"], "extra": "<script>x</script>"},
        "just a string",
        {"name": "x" * 5000, "description": "d" * 5000,
         "uses": ["u" * 500] + [f"item {i}" for i in range(50)]},
        {"name": "", "description": "no name"},
        {"name": 42, "description": "not a string name"},
        {"name": "Bare"},
        None,
        ["a", "list"],
    ]
    out = clean_suggestions(raw)

    assert all(isinstance(s, dict) for s in out)
    assert all(set(s) == {"name", "description", "uses"} for s in out)
    assert [s["name"][:11] for s in out] == ["Tomato Soup", "xxxxxxxxxxx", "Bare"]

    first, big, bare = out
    assert first == {"name": "Tomato Soup", "description": "Uses the ripe tomatoes.",
                     "uses": ["tomatoes", "onion"]}
    assert len(big["name"]) == 120
    assert len(big["description"]) == 300
    assert len(big["uses"]) == 20
    assert all(len(u) <= 80 for u in big["uses"])
    assert bare == {"name": "Bare", "description": "", "uses": []}


def test_clean_suggestions_tolerates_non_list_replies():
    from app.routers.mealie import clean_suggestions

    assert clean_suggestions(None) == []
    assert clean_suggestions("Here are some ideas") == []
    assert clean_suggestions({"name": "Soup"}) == []


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    cwd = os.getcwd()
    os.chdir(_SERVICE_DIR)
    try:
        from app.config import settings

        settings.data_dir = str(tmp_path_factory.mktemp("data"))
        from app.main import app

        settings.grocy_base_url = "http://grocy.test"
        settings.grocy_api_key = "test-grocy-key"
        settings.auth_required = False
        settings.auth_password = ""
        with TestClient(app) as c:
            yield c
    finally:
        os.chdir(cwd)


def test_suggest_llm_route_returns_clean_cards(client, monkeypatch):
    from app.config import settings
    from app.routers import mealie as mealie_router

    monkeypatch.setattr(settings, "ai_token_budget", 0)

    class _Provider:
        async def suggest_from_inventory(self, *a, **k):
            return [{"name": "Omelette", "description": "Eggs first.", "uses": ["eggs"]},
                    "a stray string",
                    {"name": "N" * 5000, "uses": "not a list"}]

    async def stock(self):
        return [{"name": "Eggs", "days_remaining": 3}]
    monkeypatch.setattr(mealie_router.GrocyClient, "get_full_stock", stock)
    monkeypatch.setattr(mealie_router, "get_enrich_provider", lambda: _Provider())

    r = client.post("/mealie/suggest/llm", json={})
    assert r.status_code == 200, r.text
    sug = r.json()["suggestions"]
    assert sug[0] == {"name": "Omelette", "description": "Eggs first.", "uses": ["eggs"]}
    assert len(sug) == 2
    assert len(sug[1]["name"]) == 120 and sug[1]["uses"] == []


def test_use_it_up_returns_clean_cards(client, monkeypatch):
    # The Expiring page's use-it-up ideas come from the same model call, so
    # they are validated the same way.
    from app.config import settings
    from app.routers import mealie as mealie_router
    from app.services.grocy import GrocyClient

    monkeypatch.setattr(settings, "ai_token_budget", 0)

    class _Provider:
        async def suggest_from_inventory(self, *a, **k):
            return [{"name": "Milk Pudding", "description": "Uses the milk.",
                     "uses": ["milk"], "extra": "dropped"},
                    "a stray string",
                    {"description": "no name"}]

    async def expiring(self, days=7):
        return [{"name": "Milk"}]
    monkeypatch.setattr(GrocyClient, "get_expiring", expiring)
    monkeypatch.setattr(mealie_router, "get_enrich_provider", lambda: _Provider())

    r = client.post("/mealie/use-it-up", json={"days": 7})
    assert r.status_code == 200, r.text
    assert r.json()["suggestions"] == [
        {"name": "Milk Pudding", "description": "Uses the milk.", "uses": ["milk"]}]
