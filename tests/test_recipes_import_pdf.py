"""PDF recipe import (FoodAssistant-wtga).

Drives the /mealie/recipes/import-pdf endpoint via TestClient with every bit of
I/O mocked: pypdf text extraction and the AI provider are monkeypatched, so no
PDF library or network is needed.

Covered:
  * a text PDF -> the AI draft comes back for review (saved: False)
  * a scanned / no-text PDF -> the friendly "no readable text" message
  * a non-PDF upload -> a clear 400
  * no AI provider configured -> 503 with the setup pointer
  * an oversized upload -> 413
"""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SERVICE_DIR = Path(__file__).parent.parent / "service"


class _FakeProvider:
    async def extract_recipe(self, page_text=None, **kwargs):
        assert page_text  # the PDF text is what reaches the provider
        return {"name": "PDF Lasagna", "servings": "4",
                "ingredients": ["pasta", "sauce"], "instructions": ["layer", "bake"]}


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
        settings.vision_provider = "gemini"
        settings.gemini_api_key = "test-gemini-key"
        settings.mealie_base_url = "http://mealie.test"
        settings.mealie_api_key = "test-mealie-key"
        settings.auth_required = False
        settings.auth_password = ""
        with TestClient(app) as c:
            yield c
    finally:
        os.chdir(cwd)


def _pdf(content=b"%PDF-1.4 fake bytes"):
    return {"file": ("recipe.pdf", content, "application/pdf")}


def test_text_pdf_yields_preview(client, monkeypatch):
    import app.dependencies as deps
    import app.services.recipes_pdf as pdf

    monkeypatch.setattr(pdf, "extract_pdf_text", lambda raw, **k: "Lasagna\n" + ("word " * 60))
    monkeypatch.setattr(deps, "get_enrich_provider", lambda: _FakeProvider())

    r = client.post("/mealie/recipes/import-pdf", files=_pdf())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["saved"] is False
    assert body["recipe"]["name"] == "PDF Lasagna"
    assert body["recipe"]["ingredients"] == ["pasta", "sauce"]


def test_image_only_pdf_friendly_message(client, monkeypatch):
    import app.services.recipes_pdf as pdf

    monkeypatch.setattr(pdf, "extract_pdf_text", lambda raw, **k: "   ")

    r = client.post("/mealie/recipes/import-pdf", files=_pdf())
    assert r.status_code == 422
    assert "no readable text" in r.json()["detail"]
    assert "photo" in r.json()["detail"]


def test_non_pdf_rejected(client):
    r = client.post("/mealie/recipes/import-pdf",
                    files={"file": ("notes.txt", b"hello", "text/plain")})
    assert r.status_code == 400
    assert "not a PDF" in r.json()["detail"]


def test_no_provider_returns_503(client, monkeypatch):
    from app.config import settings
    # ai_configured() keys off the vision provider's API key.
    monkeypatch.setattr(settings, "gemini_api_key", "")
    r = client.post("/mealie/recipes/import-pdf", files=_pdf())
    assert r.status_code == 503
    assert r.json()["detail"]["setup_url"] == "/setup"


def test_oversized_pdf_returns_413(client, monkeypatch):
    import app.services.recipes_pdf as pdf
    monkeypatch.setattr(pdf, "MAX_PDF_BYTES", 8)

    r = client.post("/mealie/recipes/import-pdf", files=_pdf(b"way too many bytes here"))
    assert r.status_code == 413
    assert "too large" in r.json()["detail"]


def test_clean_collapses_whitespace():
    # Pure helper, no pypdf needed.
    from app.services.recipes_pdf import _clean
    assert _clean("a  \t b\n\n\n c") == "a b\n c"
