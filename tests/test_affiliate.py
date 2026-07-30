"""Tests for affiliate product recommendations (FoodAssistant-k2kv): the Amazon
URL builder (search vs ASIN, empty tag), the recommendation ranking (owned items
deprioritized, recipe_missing surfaced, tag applied), and the /ui/shop render
showing the FTC disclosure."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import affiliate  # noqa: E402


# -- amazon_url -------------------------------------------------------------

def test_url_search_term_with_tag():
    url = affiliate.amazon_url("cast iron skillet", "mytag-20")
    assert url == "https://www.amazon.com/s?k=cast+iron+skillet&tag=mytag-20"


def test_url_search_term_no_tag():
    url = affiliate.amazon_url("cast iron skillet")
    assert url == "https://www.amazon.com/s?k=cast+iron+skillet"
    assert "tag=" not in url


def test_url_asin_with_tag():
    url = affiliate.amazon_url("B00FLYWNYQ", "mytag-20")
    assert url == "https://www.amazon.com/dp/B00FLYWNYQ?tag=mytag-20"


def test_url_asin_no_tag():
    url = affiliate.amazon_url("B00FLYWNYQ")
    assert url == "https://www.amazon.com/dp/B00FLYWNYQ"
    assert "tag=" not in url


def test_url_eleven_chars_is_a_search_not_asin():
    # 11 chars is not a valid ASIN, so it must be treated as a search term.
    url = affiliate.amazon_url("ABCDEFGHIJK", "t")
    assert "/s?k=" in url and "/dp/" not in url


def test_url_blank_tag_is_dropped():
    assert affiliate.amazon_url("whisk", "   ") == "https://www.amazon.com/s?k=whisk"


# -- recommendations --------------------------------------------------------

def test_owned_appliances_are_deprioritized():
    owned = ["air_fryer", "blender", "stand_mixer", "food_processor"]
    recs = affiliate.recommendations(owned, tag="")
    keys = [r["appliance_key"] for r in recs if r["appliance_key"]]
    # An un-owned appliance must appear before an owned one in the ranking.
    first_unowned = next(i for i, r in enumerate(recs)
                         if r["appliance_key"] and r["appliance_key"] not in owned)
    air_fryer_idx = next(i for i, r in enumerate(recs) if r["appliance_key"] == "air_fryer")
    assert first_unowned < air_fryer_idx
    assert "air_fryer" in keys  # owned items are still present, just later


def test_recipe_missing_surfaces_first():
    owned = ["air_fryer"]
    recs = affiliate.recommendations(owned, tag="", recipe_missing=["Slow cooker"])
    assert recs[0]["appliance_key"] == "slow_cooker"
    assert recs[0]["reason"] == "A recipe you looked at needs this"


def test_tag_is_applied_to_urls():
    recs = affiliate.recommendations([], tag="shoptag-20")
    assert recs and all("tag=shoptag-20" in r["url"] for r in recs)


def test_no_tag_means_no_tag_param():
    recs = affiliate.recommendations([], tag="")
    assert all("tag=" not in r["url"] for r in recs)


_CATEGORIES = {c for c, _label in affiliate.CATEGORY_LABELS}


def test_grouped_recommendations_cover_all_categories():
    # With a stand mixer owned, every category (including attachments) appears.
    groups = affiliate.grouped_recommendations(["stand_mixer"], tag="")
    labels = {g["category"] for g in groups}
    assert {"appliances", "attachments", "cookware", "gadgets", "storage"} <= labels
    # Every catalog item ends up in exactly one group.
    total = sum(len(g["products"]) for g in groups)
    assert total == len(affiliate.PRODUCT_CATALOG)


def test_attachments_hidden_without_a_stand_mixer():
    groups = affiliate.grouped_recommendations([], tag="")
    assert "attachments" not in {g["category"] for g in groups}
    # And present once a stand mixer is owned.
    owned_groups = affiliate.grouped_recommendations(["stand_mixer"], tag="")
    assert "attachments" in {g["category"] for g in owned_groups}


def test_catalog_has_no_fabricated_asins():
    # The catalog uses search terms only (no 10-char ASIN strings baked in).
    for item in affiliate.PRODUCT_CATALOG:
        assert not affiliate._ASIN_RE.match(item["term"])
        assert item["category"] in _CATEGORIES


def test_stand_mixer_attachments_are_in_the_catalog():
    keys = {item["appliance_key"] for item in affiliate.PRODUCT_CATALOG}
    for k in ("pasta_roller", "pasta_extruder", "sm_meat_grinder",
              "sm_pasta_roller_cutter", "sm_spiralizer"):
        assert k in keys


def test_highlighted_flag_marks_unowned_and_missing():
    recs = affiliate.recommendations(["air_fryer"], tag="", recipe_missing=["Slow cooker"])
    by_key = {r["appliance_key"]: r for r in recs if r["appliance_key"]}
    # An un-owned appliance and a recipe-missing one are highlighted.
    assert by_key["slow_cooker"]["highlighted"] is True
    assert by_key["blender"]["highlighted"] is True
    # An owned appliance is not highlighted.
    assert by_key["air_fryer"]["highlighted"] is False


def test_top_recommendations_only_highlighted_and_capped():
    picks = affiliate.top_recommendations([], tag="", recipe_missing=["Slow cooker"], limit=4)
    assert len(picks) <= 4
    assert all(p["highlighted"] for p in picks)
    # Recipe-missing pick floats to the very top.
    assert picks[0]["appliance_key"] == "slow_cooker"


def test_top_recommendations_empty_when_all_owned():
    owned = list(affiliate._missing_names_to_keys([]) | {
        item["appliance_key"] for item in affiliate.PRODUCT_CATALOG if item["appliance_key"]
    })
    assert affiliate.top_recommendations(owned, tag="") == []


# -- /ui/shop render --------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd(); os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://g", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "vision_provider", "gemini", raising=False)
    monkeypatch.setattr(settings, "gemini_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "kitchen_appliances", ["blender", "oven"], raising=False)
    # The Associates tag is now the project owner's static constant, not a
    # per-user setting; override it on the router module for the render test.
    monkeypatch.setattr("app.routers.affiliate.AMAZON_ASSOCIATES_TAG", "rendertag-20", raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_shop_page_renders_with_disclosure(client):
    r = client.get("/ui/shop")
    assert r.status_code == 200
    body = r.text
    assert "affiliate links" in body and "free and open source" in body
    # Tagged links should appear on the page.
    assert "tag=rendertag-20" in body
    assert "Recommended Kitchen Products" in body
    # The kitchen owns only blender/oven, so the un-owned picks are pinned.
    assert "Recommended for you" in body


def test_shop_page_hides_storefront_link_when_unset(monkeypatch, client):
    # With no storefront url, no storefront button renders.
    monkeypatch.setattr("app.routers.affiliate.AMAZON_STOREFRONT_URL", "", raising=False)
    r = client.get("/ui/shop")
    assert "Browse our recommended items on Amazon" not in r.text


def test_shop_page_shows_storefront_link_when_set(monkeypatch, client):
    monkeypatch.setattr(
        "app.routers.affiliate.AMAZON_STOREFRONT_URL",
        "https://www.amazon.com/shop/example",
        raising=False,
    )
    r = client.get("/ui/shop")
    assert "Browse our recommended items on Amazon" in r.text
    assert "https://www.amazon.com/shop/example" in r.text


# -- Our Picks: curated hardware (FoodAssistant-ztly) -----------------------

def test_our_picks_data_loads_and_is_well_formed():
    from app.data import our_picks as data
    assert data.OUR_PICKS, "picks list must be non-empty"
    assert data.CATEGORIES, "category order must be non-empty"
    assert isinstance(data.DISCLOSURE, str) and data.DISCLOSURE.strip()
    for p in data.OUR_PICKS:
        for field in ("name", "category", "search", "description"):
            assert str(p.get(field, "")).strip(), f"{field} required on {p}"
        # Optional fields, when present, are strings.
        assert isinstance(p.get("note", ""), str)
        assert isinstance(p.get("image", ""), str)


def test_our_picks_categories_all_known():
    from app.data import our_picks as data
    known = set(data.CATEGORIES)
    for p in data.OUR_PICKS:
        assert p["category"] in known, f"unknown category {p['category']!r}"


def test_our_picks_cover_expected_build_hardware():
    from app.data import our_picks as data
    cats = {p["category"] for p in data.OUR_PICKS}
    # A real build needs at least a Pi, a screen, a deck, and a scanner.
    assert {"Raspberry Pi", "Touchscreen", "Stream Deck", "Barcode Scanner"} <= cats


def test_build_our_picks_applies_tag_to_every_url():
    from app.data import our_picks as data
    groups = affiliate.build_our_picks(data.OUR_PICKS, data.CATEGORIES, tag="pickstag-20")
    assert groups
    urls = [item["url"] for g in groups for item in g["products"]]
    assert urls and all("tag=pickstag-20" in u for u in urls)
    # Same number of rendered products as source picks (nothing dropped).
    assert len(urls) == len(data.OUR_PICKS)


def test_build_our_picks_tag_override_changes_urls():
    from app.data import our_picks as data
    a = affiliate.build_our_picks(data.OUR_PICKS, data.CATEGORIES, tag="one-20")
    b = affiliate.build_our_picks(data.OUR_PICKS, data.CATEGORIES, tag="two-20")
    first_a = a[0]["products"][0]["url"]
    first_b = b[0]["products"][0]["url"]
    assert "tag=one-20" in first_a and "tag=two-20" in first_b
    assert first_a != first_b


def test_build_our_picks_no_tag_means_no_tag_param():
    from app.data import our_picks as data
    groups = affiliate.build_our_picks(data.OUR_PICKS, data.CATEGORIES, tag="")
    urls = [item["url"] for g in groups for item in g["products"]]
    assert urls and all("tag=" not in u for u in urls)


def test_build_our_picks_unknown_category_falls_into_last():
    picks = [{"name": "Widget", "category": "Nonsense", "search": "widget",
              "description": "d"}]
    cats = ["Raspberry Pi", "Accessories"]
    groups = affiliate.build_our_picks(picks, cats, tag="t-20")
    assert len(groups) == 1
    assert groups[0]["category"] == "Accessories"


def test_build_our_picks_groups_in_category_order_and_skips_empty():
    from app.data import our_picks as data
    groups = affiliate.build_our_picks(data.OUR_PICKS, data.CATEGORIES, tag="")
    labels = [g["category"] for g in groups]
    # Order follows CATEGORIES; only non-empty categories appear.
    assert labels == [c for c in data.CATEGORIES if c in labels]


def test_shop_page_renders_our_picks_with_tagged_links_and_disclosure(client):
    from app.data import our_picks as data
    r = client.get("/ui/shop")
    body = r.text
    assert "Our Picks" in body
    # Every category present in the data with picks renders as a group heading.
    for cat in {p["category"] for p in data.OUR_PICKS}:
        assert cat in body
    # Picks carry the render-time tag applied by the router.
    assert "tag=rendertag-20" in body
    # The Our Picks affiliate disclosure line is on the page.
    assert data.DISCLOSURE[:40] in body
