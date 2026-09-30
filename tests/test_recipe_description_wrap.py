"""Recipe descriptions stay inside their cards (FoodAssistant-gy5a).

The browse cards used an undefined min-width-0 class, so their flex column
could never shrink and a long or unbroken description pushed straight out of
the card. These tests pin the fix in the rendered pages: the shrink utility
and the two-line clamp are defined, the card templates use the clamp, and the
read views wrap without clamping. No network: only page shells are rendered.
"""
import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SERVICE_DIR = Path(__file__).parent.parent / "service"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    cwd = os.getcwd()
    os.chdir(_SERVICE_DIR)
    try:
        from app.config import settings

        data_dir = tmp_path_factory.mktemp("data")
        settings.data_dir = str(data_dir)

        from app.main import app

        settings.grocy_base_url = "http://grocy.test"
        settings.grocy_api_key = "test-grocy-key"
        settings.mealie_base_url = "http://mealie.test"
        settings.mealie_api_key = "test-mealie-key"
        settings.auth_required = False
        settings.auth_password = ""

        with TestClient(app) as c:
            yield c
    finally:
        os.chdir(cwd)


def _clamp_css_present(page: str) -> bool:
    # The clamp block: wrap-anywhere plus the two-line -webkit-line-clamp
    # pattern with its max-height fallback, on the recipe-desc class.
    return (".recipe-desc" in page
            and "overflow-wrap: anywhere" in page
            and "-webkit-line-clamp: 2" in page
            and "max-height: 3em" in page)


def test_recipes_page_defines_shrink_and_clamp(client):
    page = client.get("/ui/recipes").text
    assert re.search(r"\.min-width-0\s*\{\s*min-width:\s*0", page)
    assert _clamp_css_present(page)


def test_recipe_cards_use_the_clamp_class(client):
    page = client.get("/ui/recipes").text
    # Both card variants (library rows and external/community rows) clamp
    # their description instead of relying on single-line truncation.
    cards = page.count('class="small text-secondary recipe-desc"')
    assert cards >= 2
    assert '${r.description ? `<div class="small text-secondary recipe-desc">' in page


def test_read_view_wraps_without_clamping(client):
    page = client.get("/ui/recipes").text
    # The preview/read modal shows everything: wrap rule, no clamp selector.
    assert "#pv-description { overflow-wrap: anywhere" in page


def test_cook_page_gets_the_same_treatment(client):
    page = client.get("/ui/cook").text
    assert re.search(r"\.min-width-0\s*\{\s*min-width:\s*0", page)
    assert _clamp_css_present(page)
    assert 'class="small text-secondary mt-1 recipe-desc"' in page
    assert "#cpv-description, #aipv-description { overflow-wrap: anywhere" in page
