"""Cook affordances on the Recipes and Cook pages (FoodAssistant-j278).

External (web) recipe cards are rendered client-side from JS embedded in the
page, so these tests drive the real app via TestClient and assert on the JS
function/markup strings in the page source rather than the runtime DOM:

  * Recipes page: a Cook button for external recipes that saves to Mealie then
    makes it the Current Recipe (cookExternal), without dropping the existing
    Save (saveExternal).
  * Cook page: external suggestions in the shopping tier offer BOTH a plain Save
    (importExternal(..., false, ...)) and Save + list (importExternal(..., true,
    ...)), so saving never forces a shopping-list edit.
"""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Jinja2Templates uses the relative path "app/templates", so the app must be
# imported and run with the working directory set to service/.
_SERVICE_DIR = Path(__file__).parent.parent / "service"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    cwd = os.getcwd()
    os.chdir(_SERVICE_DIR)
    try:
        from app.config import settings

        # Point data_dir at a temp dir BEFORE importing app.main: database.py
        # runs os.makedirs(settings.data_dir) at import time.
        data_dir = tmp_path_factory.mktemp("data")
        settings.data_dir = str(data_dir)

        from app.main import app

        # Fully configured (setup-redirect middleware is a no-op) with Mealie on
        # so the recipes/cook pages render their bodies, and auth off.
        settings.grocy_base_url = "http://grocy.test"
        settings.grocy_api_key = "test-grocy-key"
        settings.vision_provider = "gemini"
        settings.gemini_api_key = "test-gemini-key"
        settings.mealie_base_url = "http://mealie.test"
        settings.mealie_api_key = "test-mealie-key"
        settings.auth_required = False
        settings.auth_password = ""
        assert settings.is_configured()
        assert settings.mealie_configured()

        with TestClient(app) as c:
            yield c
    finally:
        os.chdir(cwd)


def test_recipes_page_has_cook_external(client):
    page = client.get("/ui/recipes").text
    # New Cook flow for external recipes...
    assert "cookExternal(" in page
    assert "function cookExternal(" in page
    # ...chains save (import-external) then making it the current recipe.
    assert "mealie/recipes/import-external" in page
    assert "current-recipe/from-mealie" in page
    assert "ui/current-recipe" in page
    # ...and the existing Save affordance is still present.
    assert "saveExternal(" in page


def test_recipes_preview_modal_has_cook(client):
    page = client.get("/ui/recipes").text
    # The external preview modal gains a Cook button wired to cookFromPreview.
    assert 'id="pv-cook"' in page
    assert "cookFromPreview(" in page


def test_cook_page_external_shopping_has_save_and_save_plus_list(client):
    page = client.get("/ui/cook").text
    # External shopping-tier markup renders both a plain Save and a Save + list.
    assert "importExternal('${s.external_id}', '${s.source}', false, this)" in page
    assert "importExternal('${s.external_id}', '${s.source}', true, this)" in page
    # Labels/icons stay consistent with the existing buttons.
    assert "Save + list" in page
    assert "bi-cart-plus" in page
