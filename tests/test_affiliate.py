"""Tests for the Shop page's product picks (FoodAssistant-k2kv): the picks page
link builder, the recommendation ranking (owned items deprioritized,
recipe_missing surfaced), and the /ui/shop render.

Amazon Associates policy keeps tagged links out of installable software, so
every pick links to https://pantryraider.app/gear and no Amazon link or
affiliate tag ever reaches the page (FoodAssistant-nlm1n.2)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import affiliate  # noqa: E402

GEAR = "https://pantryraider.app/gear"


# -- gear_url ----------------------------------------------------------------

def test_gear_url_is_the_website_picks_page():
    from app.config import GEAR_URL
    assert GEAR_URL == GEAR
    assert affiliate.gear_url() == GEAR


def test_gear_url_anchors_on_the_product_name():
    assert affiliate.gear_url("Cast iron skillet") == GEAR + "#cast-iron-skillet"
    assert affiliate.gear_url("Pressure cooker (Instant Pot)") == (
        GEAR + "#pressure-cooker-instant-pot")
    assert affiliate.gear_anchor("Chef's knife") == "chef-s-knife"
    assert affiliate.gear_anchor("Elgato Stream Deck MK.2 (15-key)") == (
        "elgato-stream-deck-mk-2-15-key")


def test_every_anchor_is_unique():
    from app.data.our_picks import OUR_PICKS
    names = [i["name"] for i in affiliate.PRODUCT_CATALOG] + [p["name"] for p in OUR_PICKS]
    anchors = [affiliate.gear_anchor(n) for n in names]
    assert all(anchors) and len(set(anchors)) == len(anchors)


def test_no_tag_and_no_store_link_in_the_app():
    # Neither the config nor any template, service or router builds an Amazon
    # link or carries an Associates tag.
    import app.config as config
    assert not hasattr(config, "AMAZON_ASSOCIATES_TAG")
    assert not hasattr(config, "AMAZON_STOREFRONT_URL")
    offenders = []
    for path in (SERVICE / "app").rglob("*"):
        if path.suffix not in (".py", ".html", ".js") or "vendor" in path.parts:
            continue
        text = path.read_text(errors="ignore").lower()
        for needle in ("amazon.com", "amzn.", "improvisedeng", "tag=improvised"):
            if needle in text:
                offenders.append(f"{path.relative_to(SERVICE)}: {needle}")
    assert not offenders, offenders


def test_docs_and_demo_carry_no_tagged_links():
    docs = SERVICE.parent / "docs"
    offenders = []
    for path in docs.rglob("*"):
        if path.suffix not in (".md", ".html", ".js") or "design" in path.parts:
            continue
        text = path.read_text(errors="ignore").lower()
        for needle in ("improvisedeng", "amazon.com/shop", "amzn.to", "&tag=", "?tag="):
            if needle in text:
                offenders.append(f"{path.relative_to(docs)}: {needle}")
    assert not offenders, offenders


# -- recommendations --------------------------------------------------------

def test_owned_appliances_are_deprioritized():
    owned = ["air_fryer", "blender", "stand_mixer", "food_processor"]
    recs = affiliate.recommendations(owned)
    keys = [r["appliance_key"] for r in recs if r["appliance_key"]]
    # An un-owned appliance must appear before an owned one in the ranking.
    first_unowned = next(i for i, r in enumerate(recs)
                         if r["appliance_key"] and r["appliance_key"] not in owned)
    air_fryer_idx = next(i for i, r in enumerate(recs) if r["appliance_key"] == "air_fryer")
    assert first_unowned < air_fryer_idx
    assert "air_fryer" in keys  # owned items are still present, just later


def test_recipe_missing_surfaces_first():
    owned = ["air_fryer"]
    recs = affiliate.recommendations(owned, recipe_missing=["Slow cooker"])
    assert recs[0]["appliance_key"] == "slow_cooker"
    assert recs[0]["reason"] == "A recipe you looked at needs this"


def test_every_recommendation_links_to_the_picks_page():
    recs = affiliate.recommendations([])
    assert recs and all(r["url"].startswith(GEAR + "#") for r in recs)
    assert all(r["url"] == affiliate.gear_url(r["name"]) for r in recs)


_CATEGORIES = {c for c, _label in affiliate.CATEGORY_LABELS}


def test_grouped_recommendations_cover_all_categories():
    # With a stand mixer owned, every category (including attachments) appears.
    groups = affiliate.grouped_recommendations(["stand_mixer"])
    labels = {g["category"] for g in groups}
    assert {"appliances", "attachments", "cookware", "gadgets", "storage"} <= labels
    # Every catalog item ends up in exactly one group.
    total = sum(len(g["products"]) for g in groups)
    assert total == len(affiliate.PRODUCT_CATALOG)


def test_attachments_hidden_without_a_stand_mixer():
    groups = affiliate.grouped_recommendations([])
    assert "attachments" not in {g["category"] for g in groups}
    # And present once a stand mixer is owned.
    owned_groups = affiliate.grouped_recommendations(["stand_mixer"])
    assert "attachments" in {g["category"] for g in owned_groups}


def test_catalog_categories_are_known():
    for item in affiliate.PRODUCT_CATALOG:
        assert item["term"].strip()
        assert item["category"] in _CATEGORIES


def test_stand_mixer_attachments_are_in_the_catalog():
    keys = {item["appliance_key"] for item in affiliate.PRODUCT_CATALOG}
    for k in ("pasta_roller", "pasta_extruder", "sm_meat_grinder",
              "sm_pasta_roller_cutter", "sm_spiralizer"):
        assert k in keys


def test_highlighted_flag_marks_unowned_and_missing():
    recs = affiliate.recommendations(["air_fryer"], recipe_missing=["Slow cooker"])
    by_key = {r["appliance_key"]: r for r in recs if r["appliance_key"]}
    # An un-owned appliance and a recipe-missing one are highlighted.
    assert by_key["slow_cooker"]["highlighted"] is True
    assert by_key["blender"]["highlighted"] is True
    # An owned appliance is not highlighted.
    assert by_key["air_fryer"]["highlighted"] is False


def test_top_recommendations_only_highlighted_and_capped():
    picks = affiliate.top_recommendations([], recipe_missing=["Slow cooker"], limit=4)
    assert len(picks) <= 4
    assert all(p["highlighted"] for p in picks)
    # Recipe-missing pick floats to the very top.
    assert picks[0]["appliance_key"] == "slow_cooker"


def test_top_recommendations_empty_when_all_owned():
    owned = list(affiliate._missing_names_to_keys([]) | {
        item["appliance_key"] for item in affiliate.PRODUCT_CATALOG if item["appliance_key"]
    })
    assert affiliate.top_recommendations(owned) == []


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
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_shop_page_renders_with_its_note(client):
    r = client.get("/ui/shop")
    assert r.status_code == 200
    body = r.text
    # The note tracks the licence (PolyForm Noncommercial), so it must not
    # claim the project is open source.
    assert "free for personal and non-commercial use" in body
    assert "open source" not in body
    assert "Recommended Kitchen Products" in body
    # The kitchen owns only blender/oven, so the un-owned picks are pinned.
    assert "Recommended for you" in body


def test_shop_page_links_only_to_the_picks_page(client):
    body = client.get("/ui/shop").text
    assert f'href="{GEAR}"' in body
    assert f'href="{GEAR}#air-fryer"' in body
    low = body.lower()
    assert "amazon.com" not in low and "tag=" not in low
    assert "sponsored" not in low


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


def test_build_our_picks_links_every_pick_to_the_picks_page():
    from app.data import our_picks as data
    groups = affiliate.build_our_picks(data.OUR_PICKS, data.CATEGORIES)
    assert groups
    items = [item for g in groups for item in g["products"]]
    assert items and all(i["url"] == affiliate.gear_url(i["name"]) for i in items)
    # Same number of rendered products as source picks (nothing dropped).
    assert len(items) == len(data.OUR_PICKS)


def test_build_our_picks_unknown_category_falls_into_last():
    picks = [{"name": "Widget", "category": "Nonsense", "search": "widget",
              "description": "d"}]
    cats = ["Raspberry Pi", "Accessories"]
    groups = affiliate.build_our_picks(picks, cats)
    assert len(groups) == 1
    assert groups[0]["category"] == "Accessories"


def test_build_our_picks_groups_in_category_order_and_skips_empty():
    from app.data import our_picks as data
    groups = affiliate.build_our_picks(data.OUR_PICKS, data.CATEGORIES)
    labels = [g["category"] for g in groups]
    # Order follows CATEGORIES; only non-empty categories appear.
    assert labels == [c for c in data.CATEGORIES if c in labels]


def test_shop_page_renders_our_picks_with_their_note(client):
    from app.data import our_picks as data
    r = client.get("/ui/shop")
    body = r.text
    assert "Our Picks" in body
    # Every category present in the data with picks renders as a group heading.
    for cat in {p["category"] for p in data.OUR_PICKS}:
        assert cat in body
    assert f'href="{GEAR}#raspberry-pi-4-model-b-4gb"' in body
    # The Our Picks note is on the page.
    assert data.DISCLOSURE[:40] in body
