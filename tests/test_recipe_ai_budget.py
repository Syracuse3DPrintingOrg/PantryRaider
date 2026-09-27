"""The monthly AI token budget also covers the recipe and nutrition AI routes.

Settings promises that AI features pause once the month's budget is spent, but
recipe generate, AI suggestions, web page, photo and PDF extraction, optimize,
ingredient parsing, and the nutrition estimate all called the paid provider
anyway. Each must now answer 429 with the budget message before any provider
call, and a save quietly skips the AI ingredient parse instead.

The budget is spent for real (a recorded usage over a small budget in a
temporary data dir), and the provider is a stub that records every call.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SERVICE_DIR = Path(__file__).parent.parent / "service"
_TAG = uuid.uuid4().hex[:8]


class _RecordingProvider:
    """Stands in for every provider; any AI method call is recorded."""

    def __init__(self):
        self.calls: list[str] = []

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        async def _call(*args, **kwargs):
            self.calls.append(name)
            return None
        return _call


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
        settings.mealie_base_url = ""
        settings.mealie_api_key = ""
        settings.recipes_backend = "native"
        settings.auth_required = False
        settings.auth_password = ""
        with TestClient(app) as c:
            yield c
    finally:
        os.chdir(cwd)


@pytest.fixture
def spent(client, monkeypatch, tmp_path):
    """An AI provider is set up, this month's budget is used up, and every
    provider entry point hands back the recording stub."""
    from app import dependencies
    from app.config import settings
    from app.routers import mealie as mealie_router
    from app.services import usage

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "vision_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(settings, "ai_token_budget", 100)
    usage.record("gemini", 500)
    assert usage.over_budget()

    provider = _RecordingProvider()
    monkeypatch.setattr(dependencies, "get_enrich_provider", lambda: provider)
    monkeypatch.setattr(dependencies, "get_vision_provider", lambda: provider)
    monkeypatch.setattr(mealie_router, "get_enrich_provider", lambda: provider)
    return provider


def _budget_refusal(r):
    assert r.status_code == 429, r.text
    body = r.json()
    assert "budget" in (body.get("detail") or body.get("error") or "").lower()


def test_generate_refused_over_budget(client, spent):
    _budget_refusal(client.post("/mealie/recipes/generate", json={"name": "Soup"}))
    assert spent.calls == []


def test_suggest_llm_refused_over_budget(client, spent, monkeypatch):
    from app.routers import mealie as mealie_router

    async def stock(self):
        return [{"name": "Milk", "days_remaining": 2}]
    monkeypatch.setattr(mealie_router.GrocyClient, "get_full_stock", stock)
    _budget_refusal(client.post("/mealie/suggest/llm", json={}))
    assert spent.calls == []


def test_url_extraction_refused_over_budget(client, spent, monkeypatch):
    from app.services import egress, recipe_scrape

    async def no_structured_data(url):
        raise recipe_scrape.RecipeScrapeError("no recipe data")
    fetched = []

    def no_fetch(**kwargs):
        fetched.append(kwargs)
        raise AssertionError("the page must not be fetched over budget")
    monkeypatch.setattr(egress, "is_safe_public_url", lambda url, **k: True)
    monkeypatch.setattr(egress, "guarded_async_client", no_fetch)
    monkeypatch.setattr(recipe_scrape, "scrape_url", no_structured_data)
    _budget_refusal(client.post("/mealie/recipes/import-url",
                                json={"url": "https://recipes.example/pie"}))
    assert spent.calls == []
    assert fetched == []


def test_photo_extraction_refused_over_budget(client, spent):
    _budget_refusal(client.post(
        "/mealie/recipes/extract-photo",
        files={"file": ("card.jpg", b"\xff\xd8\xff fake jpeg", "image/jpeg")}))
    assert spent.calls == []


def test_pdf_import_refused_over_budget(client, spent, monkeypatch):
    import app.services.recipes_pdf as pdf
    monkeypatch.setattr(pdf, "extract_pdf_text", lambda raw, **k: "Pie. " * 200)
    _budget_refusal(client.post(
        "/mealie/recipes/import-pdf",
        files={"file": ("pie.pdf", b"%PDF-1.4 fake", "application/pdf")}))
    assert spent.calls == []


def test_optimize_refused_over_budget(client, spent):
    _budget_refusal(client.post("/mealie/recipes/optimize", json={
        "name": "Pie", "ingredients": ["flour"], "instructions": ["Bake."]}))
    assert spent.calls == []


def test_parse_ingredients_refused_over_budget(client, spent):
    from app.database import SessionLocal
    from app.services import recipe_store

    db = SessionLocal()
    try:
        saved = recipe_store.create_from_parsed(db, {
            "name": f"Budget Pie {_TAG}", "ingredients": ["2 cups flour"],
            "instructions": ["Bake."]})
    finally:
        db.close()
    try:
        _budget_refusal(client.post("/mealie/recipes/parse-ingredients",
                                    json={"slug": saved["slug"]}))
        assert spent.calls == []
    finally:
        client.delete(f"/recipes/{saved['slug']}")


def test_nutrition_estimate_refused_over_budget(client, spent):
    r = client.post("/nutrition/estimate", json={"name": "apple"})
    _budget_refusal(r)
    # The log form reads {ok, error}, so the reason reaches the page.
    assert r.json()["ok"] is False
    assert spent.calls == []


def test_use_it_up_skips_ai_over_budget(client, spent, monkeypatch):
    # Use-it-up keeps its static tips and says why there are no AI ideas.
    from app.services.grocy import GrocyClient

    async def expiring(self, days=7):
        return [{"name": "Milk"}]
    monkeypatch.setattr(GrocyClient, "get_expiring", expiring)
    r = client.post("/mealie/use-it-up", json={"days": 7})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["items"] == ["Milk"]
    assert d["tips"] and d["suggestions"] == []
    assert "budget" in d["message"].lower()
    assert spent.calls == []


def test_save_skips_ai_ingredient_parse_over_budget(client, spent):
    from app.services.mealie import build_recipe_ingredients

    out = asyncio.run(build_recipe_ingredients(["2 cups flour", "1 egg"]))
    assert out == [{"note": "2 cups flour"}, {"note": "1 egg"}]
    assert spent.calls == []


def test_under_budget_still_calls_the_provider(client, spent, monkeypatch):
    # Control: with room in the budget the same route reaches the provider.
    from app.config import settings
    monkeypatch.setattr(settings, "ai_token_budget", 0)
    r = client.post("/mealie/recipes/generate", json={"name": "Soup"})
    assert r.status_code == 502  # the stub returns no recipe
    assert spent.calls == ["generate_recipe"]


def test_provider_errors_are_not_echoed(client, spent, monkeypatch):
    # AI-11: a provider exception is logged, and the page gets fixed copy
    # instead of the raw exception text.
    from app.config import settings
    from app.routers import mealie as mealie_router

    monkeypatch.setattr(settings, "ai_token_budget", 0)

    class _Boom:
        async def generate_recipe(self, *a, **k):
            raise RuntimeError("secret upstream detail sk-live-123")

        async def suggest_from_inventory(self, *a, **k):
            raise RuntimeError("secret upstream detail sk-live-123")

        async def estimate_nutrition(self, *a, **k):
            raise RuntimeError("secret upstream detail sk-live-123")

        async def optimize_recipe(self, *a, **k):
            raise RuntimeError("secret upstream detail sk-live-123")

    boom = _Boom()
    from app import dependencies
    monkeypatch.setattr(dependencies, "get_enrich_provider", lambda: boom)
    monkeypatch.setattr(mealie_router, "get_enrich_provider", lambda: boom)

    async def stock(self):
        return [{"name": "Milk", "days_remaining": 2}]
    monkeypatch.setattr(mealie_router.GrocyClient, "get_full_stock", stock)

    for r in (
        client.post("/mealie/recipes/generate", json={"name": "Soup"}),
        client.post("/mealie/suggest/llm", json={}),
        client.post("/mealie/recipes/optimize", json={
            "name": "Pie", "ingredients": ["flour"], "instructions": ["Bake."]}),
        client.post("/nutrition/estimate", json={"name": "apple"}),
    ):
        text = r.text
        assert "sk-live-123" not in text and "secret upstream" not in text, text
        assert "LLM error" not in text and "AI error" not in text
