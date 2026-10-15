"""Forager's AI proxy reads photos, receipts and barcodes, but it does not write
recipes, recipe ideas or nutrition estimates (FoodAssistant-nlm1n.9).

A kitchen whose AI is Forager used to see Generate, Ask AI, Optimize, recipe
import from a photo or PDF, and Estimate with AI, and each one ended in a 502
because the cloud provider answers None. Now:

- Settings.ai_writes_recipes() is False for Forager (per role, so a separate
  enrich provider still writes), and a relaying satellite takes its server's
  answer from the relay block.
- theme_context carries ai_can_write_recipes, and the pages hide the writing
  buttons behind it and show a short note instead.
- Every writing route answers 409 with that note before any provider call.
"""
from __future__ import annotations

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
def forager(client, monkeypatch, tmp_path):
    """This kitchen's AI is Forager, and every provider entry point hands
    back the recording stub."""
    from app import dependencies
    from app.config import settings
    from app.routers import mealie as mealie_router

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "vision_provider", "cloud")
    monkeypatch.setattr(settings, "enrich_provider", "")
    monkeypatch.setattr(settings, "cloud_instance_token", "test-instance-token")
    monkeypatch.setattr(settings, "ai_token_budget", 0)
    provider = _RecordingProvider()
    monkeypatch.setattr(dependencies, "get_enrich_provider", lambda: provider)
    monkeypatch.setattr(dependencies, "get_vision_provider", lambda: provider)
    monkeypatch.setattr(mealie_router, "get_enrich_provider", lambda: provider)
    return provider


def _refused(r):
    from app.config import AI_WRITING_UNAVAILABLE_MSG

    assert r.status_code == 409, r.text
    body = r.json()
    assert (body.get("detail") or body.get("error")) == AI_WRITING_UNAVAILABLE_MSG


# --- the capability ----------------------------------------------------------


def test_forager_does_not_write(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "deployment_mode", "server")
    monkeypatch.setattr(settings, "vision_provider", "cloud")
    monkeypatch.setattr(settings, "enrich_provider", "")
    assert settings.ai_writes_recipes() is False
    assert settings.ai_writes_recipes("vision") is False


@pytest.mark.parametrize("name", ["gemini", "openai", "anthropic", "ollama"])
def test_every_other_provider_writes(monkeypatch, name):
    from app.config import settings

    monkeypatch.setattr(settings, "deployment_mode", "server")
    monkeypatch.setattr(settings, "vision_provider", name)
    monkeypatch.setattr(settings, "enrich_provider", "")
    assert settings.ai_writes_recipes() is True
    assert settings.ai_writes_recipes("vision") is True


def test_a_separate_text_provider_still_writes(monkeypatch):
    # Photos through Forager, text through a key of the kitchen's own.
    from app.config import settings

    monkeypatch.setattr(settings, "deployment_mode", "server")
    monkeypatch.setattr(settings, "vision_provider", "cloud")
    monkeypatch.setattr(settings, "enrich_provider", "gemini")
    assert settings.ai_writes_recipes() is True
    assert settings.ai_writes_recipes("vision") is False


def test_relay_block_carries_the_answer(monkeypatch):
    from app.config import settings
    from app.services import fleet_relay

    monkeypatch.setattr(settings, "deployment_mode", "server")
    monkeypatch.setattr(settings, "vision_provider", "cloud")
    monkeypatch.setattr(settings, "enrich_provider", "")
    assert fleet_relay.fleet_block(settings)["writing_available"] is False
    monkeypatch.setattr(settings, "vision_provider", "gemini")
    assert fleet_relay.fleet_block(settings)["writing_available"] is True


def test_satellite_follows_its_server(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(settings, "vision_provider", "gemini")
    block = {"v": 1, "caps": ["ai"], "ai_available": True}
    monkeypatch.setattr(settings, "fleet_relay", dict(block, writing_available=False))
    assert settings.ai_writes_recipes() is False
    monkeypatch.setattr(settings, "fleet_relay", dict(block, writing_available=True))
    assert settings.ai_writes_recipes() is True
    # A server from before the field keeps the old behavior.
    monkeypatch.setattr(settings, "fleet_relay", block)
    assert settings.ai_writes_recipes() is True


# --- the routes --------------------------------------------------------------


def test_generate_refused(client, forager):
    _refused(client.post("/mealie/recipes/generate", json={"name": "Soup"}))
    assert forager.calls == []


def test_suggest_llm_refused(client, forager):
    _refused(client.post("/mealie/suggest/llm", json={}))
    assert forager.calls == []


def test_nutrition_estimate_refused(client, forager):
    r = client.post("/nutrition/estimate", json={"name": "apple"})
    _refused(r)
    assert r.json()["ok"] is False
    assert forager.calls == []


def test_optimize_refused(client, forager):
    _refused(client.post("/mealie/recipes/optimize", json={
        "name": "Pie", "ingredients": ["flour"], "instructions": ["Bake."]}))
    assert forager.calls == []


def test_photo_extraction_refused(client, forager):
    _refused(client.post(
        "/mealie/recipes/extract-photo",
        files={"file": ("card.jpg", b"\xff\xd8\xff fake jpeg", "image/jpeg")}))
    assert forager.calls == []


def test_pdf_import_refused(client, forager):
    _refused(client.post(
        "/mealie/recipes/import-pdf",
        files={"file": ("pie.pdf", b"%PDF-1.4 fake", "application/pdf")}))
    assert forager.calls == []


def test_url_ai_fallback_refused(client, forager, monkeypatch):
    from app.services import egress, recipe_scrape

    async def no_structured_data(url):
        raise recipe_scrape.RecipeScrapeError("no recipe data")

    def no_fetch(**kwargs):
        raise AssertionError("the page must not be fetched for Forager")
    monkeypatch.setattr(egress, "is_safe_public_url", lambda url, **k: True)
    monkeypatch.setattr(egress, "guarded_async_client", no_fetch)
    monkeypatch.setattr(recipe_scrape, "scrape_url", no_structured_data)
    _refused(client.post("/mealie/recipes/import-url",
                         json={"url": "https://recipes.example/pie"}))
    assert forager.calls == []


def test_parse_ingredients_refused(client, forager):
    _refused(client.post("/mealie/recipes/parse-ingredients",
                         json={"slug": f"anything-{_TAG}"}))
    assert forager.calls == []


def test_use_it_up_keeps_its_tips(client, forager, monkeypatch):
    from app.config import AI_WRITING_UNAVAILABLE_MSG
    from app.services.grocy import GrocyClient

    async def expiring(self, days=7):
        return [{"name": "Milk"}]
    monkeypatch.setattr(GrocyClient, "get_expiring", expiring)
    r = client.post("/mealie/use-it-up", json={"days": 7})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["tips"] and d["suggestions"] == []
    assert d["message"] == AI_WRITING_UNAVAILABLE_MSG
    assert forager.calls == []


# --- the pages ---------------------------------------------------------------


def test_cook_page_hides_writing_buttons(client, forager, monkeypatch):
    from app.routers import mealie as mealie_router

    async def stock(self):
        return []
    monkeypatch.setattr(mealie_router.GrocyClient, "get_full_stock", stock,
                        raising=False)
    html = client.get("/ui/cook").text
    assert 'id="btn-ask-ai"' not in html
    assert 'id="btn-copycat"' not in html
    assert "does not write recipes" in html


def test_cook_page_offers_writing_with_a_key(client, forager, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "vision_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    html = client.get("/ui/cook").text
    assert 'id="btn-ask-ai"' in html
    assert "does not write recipes" not in html


def test_nutrition_page_hides_estimate(client, forager):
    html = client.get("/ui/nutrition").text
    assert "estimateMacros" not in html
    assert "does not write recipes" in html


def test_recipes_page_hides_generate_and_photo(client, forager):
    html = client.get("/ui/recipes").text
    assert 'id="btn-generate-ai"' not in html
    assert "photo-input')" not in html
    assert 'id="editor-optimize"' not in html
    assert "does not write recipes" in html
