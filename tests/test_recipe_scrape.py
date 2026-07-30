"""In-process URL scraping wrapper (FoodAssistant-zwwe).

recipe-scrapers itself is mocked (a fake module injected into sys.modules), so
these tests pin the wrapper's mapping and failure contract without the library
or any network: scraped fields reduce to the normalized parsed-recipe dict,
missing per-field data is tolerated, an unreadable page raises the clean
RecipeScrapeError, and the wild-mode fallback covers older library signatures.
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "service"))

from app.services import recipe_scrape  # noqa: E402
from app.services.recipe_scrape import (  # noqa: E402
    RecipeScrapeError, _time_text, parse_html)


class FakeScraper:
    def __init__(self, fields: dict):
        self._fields = fields

    def __getattr__(self, name):
        def method():
            if name in self._fields:
                return self._fields[name]
            raise NotImplementedError(name)
        return method


@pytest.fixture()
def fake_lib(monkeypatch):
    """Install a fake recipe_scrapers module whose scrape_html we control."""
    mod = types.ModuleType("recipe_scrapers")
    monkeypatch.setitem(sys.modules, "recipe_scrapers", mod)
    return mod


FULL_FIELDS = {
    "title": "Weeknight Chili",
    "description": "A quick pantry chili.",
    "yields": "4 servings",
    "total_time": 45,
    "ingredients": ["2 cups kidney beans", "1 tbsp chili powder"],
    "instructions_list": ["Simmer the beans.", "Season and serve."],
    "canonical_url": "https://example.com/chili",
    "image": "https://example.com/chili.jpg",
}


def test_time_text():
    assert _time_text(45) == "45 minutes"
    assert _time_text(90) == "1 hr 30 min"
    assert _time_text(120) == "2 hr"
    assert _time_text(0) == ""
    assert _time_text(None) == ""
    assert _time_text("nope") == ""


def test_parse_html_full_mapping(fake_lib):
    fake_lib.scrape_html = lambda html, org_url, supported_only=True: FakeScraper(FULL_FIELDS)
    parsed = parse_html("<html>page</html>", "https://example.com/chili?utm=1")
    assert parsed["name"] == "Weeknight Chili"
    assert parsed["description"] == "A quick pantry chili."
    assert parsed["servings"] == "4 servings"
    assert parsed["total_time"] == "45 minutes"
    assert parsed["ingredients"] == FULL_FIELDS["ingredients"]
    assert parsed["instructions"] == FULL_FIELDS["instructions_list"]
    assert parsed["source_url"] == "https://example.com/chili"
    assert parsed["image"] == "https://example.com/chili.jpg"


def test_parse_html_tolerates_missing_fields(fake_lib):
    fields = {
        "title": "Bare Minimum",
        "ingredients": ["one thing"],
        "instructions": "Step one.\nStep two.",
    }
    fake_lib.scrape_html = lambda html, org_url, supported_only=True: FakeScraper(fields)
    parsed = parse_html("<html></html>", "https://example.com/r")
    assert parsed["name"] == "Bare Minimum"
    assert parsed["instructions"] == ["Step one.", "Step two."]
    assert parsed["description"] == ""
    assert parsed["total_time"] == ""
    assert parsed["image"] is None
    assert parsed["source_url"] == "https://example.com/r"


def test_parse_html_no_recipe_raises(fake_lib):
    fake_lib.scrape_html = lambda html, org_url, supported_only=True: FakeScraper({})
    with pytest.raises(RecipeScrapeError):
        parse_html("<html>not a recipe</html>", "https://example.com/x")


def test_parse_html_scraper_error_raises_clean(fake_lib):
    def boom(html, org_url, supported_only=True):
        raise ValueError("no schema found")
    fake_lib.scrape_html = boom
    with pytest.raises(RecipeScrapeError):
        parse_html("<html></html>", "https://example.com/x")


def test_parse_html_wild_mode_fallback_for_old_library(fake_lib):
    """An older recipe-scrapers without supported_only= is retried with wild_mode."""
    calls = []

    def old_signature(html, org_url, wild_mode=False):
        calls.append(wild_mode)
        return FakeScraper(FULL_FIELDS)

    def dispatch(html, org_url, **kwargs):
        if "supported_only" in kwargs:
            raise TypeError("unexpected keyword argument 'supported_only'")
        return old_signature(html, org_url, **kwargs)

    fake_lib.scrape_html = dispatch
    parsed = parse_html("<html></html>", "https://example.com/r")
    assert parsed["name"] == "Weeknight Chili"
    assert calls == [True]


def test_scrape_url_rejects_non_url():
    with pytest.raises(RecipeScrapeError):
        asyncio.run(recipe_scrape.scrape_url("not-a-url"))
    with pytest.raises(RecipeScrapeError):
        asyncio.run(recipe_scrape.scrape_url(""))


def test_scrape_url_maps_http_errors(fake_lib, monkeypatch):
    import httpx

    def handler(request):
        return httpx.Response(403)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def patched_client(**kwargs):
        kwargs["transport"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_client)
    with pytest.raises(RecipeScrapeError) as exc:
        asyncio.run(recipe_scrape.scrape_url("https://blocked.example/r"))
    assert "blocked" in str(exc.value)


def test_scrape_url_fetches_then_parses(fake_lib, monkeypatch):
    import httpx

    fake_lib.scrape_html = lambda html, org_url, supported_only=True: FakeScraper(FULL_FIELDS)

    def handler(request):
        return httpx.Response(200, text="<html>recipe page</html>")

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def patched_client(**kwargs):
        kwargs["transport"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_client)
    parsed = asyncio.run(recipe_scrape.scrape_url("https://example.com/chili"))
    assert parsed["name"] == "Weeknight Chili"
