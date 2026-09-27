"""PDF recipe import (FoodAssistant-wtga).

Drives the /mealie/recipes/import-pdf endpoint via TestClient with every bit of
I/O mocked: pypdf text extraction and the AI provider are monkeypatched, so no
PDF library or network is needed.

Covered:
  * a text PDF -> the AI draft comes back for review (saved: False)
  * a scanned / no-text PDF with AI -> pages rendered and read by the vision AI
  * a scanned PDF with AI off -> the friendly set-up / try-a-photo message
  * a non-PDF upload -> a clear 400
  * no AI provider configured -> 503 with the setup pointer
  * an oversized upload -> 413
  * rendering or the AI failing -> a soft, friendly message (no 500)
"""
import os
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SERVICE_DIR = Path(__file__).parent.parent / "service"


class _FakeProvider:
    async def extract_recipe(self, page_text=None, **kwargs):
        assert page_text  # the PDF text is what reaches the provider
        return {"name": "PDF Lasagna", "servings": "4",
                "ingredients": ["pasta", "sauce"], "instructions": ["layer", "bake"]}


class _FakeVisionProvider:
    """Stands in for the vision AI: records the page images it was handed and
    returns one recipe fragment per page, so the merge is exercised too."""
    def __init__(self):
        self.calls: list[bytes] = []

    async def extract_recipe(self, image_data=None, mime_type=None, **kwargs):
        assert image_data  # a rendered page image is what reaches the vision AI
        self.calls.append(image_data)
        if len(self.calls) == 1:
            return {"name": "Scanned Stew", "servings": "6",
                    "ingredients": ["beef"], "instructions": ["brown the beef"]}
        return {"name": "", "ingredients": ["broth"], "instructions": ["simmer"]}


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


def test_scanned_pdf_read_by_vision_ai(client, monkeypatch):
    # A scan has no readable text, so with AI configured the pages are rendered
    # and read by the vision AI, and the per-page drafts merge into one recipe
    # (FoodAssistant-k61s).
    import app.dependencies as deps
    import app.services.recipes_pdf as pdf

    vision = _FakeVisionProvider()
    monkeypatch.setattr(pdf, "extract_pdf_text", lambda raw, **k: "   ")
    monkeypatch.setattr(pdf, "render_pdf_pages", lambda raw, **k: [b"page1", b"page2"])
    monkeypatch.setattr(deps, "get_vision_provider", lambda: vision)

    r = client.post("/mealie/recipes/import-pdf", files=_pdf())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["saved"] is False
    assert body["recipe"]["name"] == "Scanned Stew"
    # Both pages reached the vision AI and their ingredients merged in order.
    assert vision.calls == [b"page1", b"page2"]
    assert body["recipe"]["ingredients"] == ["beef", "broth"]
    assert "scanned" in body["message"].lower()


def test_scanned_pdf_without_ai_returns_setup_pointer(client, monkeypatch):
    # With no AI provider, a scanned PDF cannot be read; the endpoint points the
    # user at setting up AI rather than dead-ending.
    from app.config import settings
    monkeypatch.setattr(settings, "gemini_api_key", "")

    r = client.post("/mealie/recipes/import-pdf", files=_pdf())
    assert r.status_code == 503
    assert r.json()["detail"]["setup_url"] == "/setup"


def test_scanned_pdf_render_failure_fails_soft(client, monkeypatch):
    # If rendering the pages blows up, the user gets a friendly message, not a 500.
    import app.services.recipes_pdf as pdf

    def _boom(raw, **k):
        raise RuntimeError("renderer exploded")

    monkeypatch.setattr(pdf, "extract_pdf_text", lambda raw, **k: "   ")
    monkeypatch.setattr(pdf, "render_pdf_pages", _boom)

    r = client.post("/mealie/recipes/import-pdf", files=_pdf())
    assert r.status_code == 422
    assert "photo" in r.json()["detail"]


def test_scanned_pdf_ai_failure_fails_soft(client, monkeypatch):
    # If the vision AI errors while reading the pages, fail soft with a 502.
    import app.dependencies as deps
    import app.services.recipes_pdf as pdf

    class _Broken:
        async def extract_recipe(self, **kwargs):
            raise RuntimeError("model unavailable")

    monkeypatch.setattr(pdf, "extract_pdf_text", lambda raw, **k: "   ")
    monkeypatch.setattr(pdf, "render_pdf_pages", lambda raw, **k: [b"page1"])
    monkeypatch.setattr(deps, "get_vision_provider", lambda: _Broken())

    r = client.post("/mealie/recipes/import-pdf", files=_pdf())
    assert r.status_code == 502


def test_text_pdf_never_calls_vision(client, monkeypatch):
    # A normal text PDF stays on the text path: the renderer and vision AI are
    # never touched.
    import app.dependencies as deps
    import app.services.recipes_pdf as pdf

    def _should_not_render(*a, **k):
        raise AssertionError("render_pdf_pages must not run for a text PDF")

    monkeypatch.setattr(pdf, "extract_pdf_text", lambda raw, **k: "Lasagna\n" + ("word " * 60))
    monkeypatch.setattr(pdf, "render_pdf_pages", _should_not_render)
    monkeypatch.setattr(deps, "get_enrich_provider", lambda: _FakeProvider())

    r = client.post("/mealie/recipes/import-pdf", files=_pdf())
    assert r.status_code == 200, r.text
    assert r.json()["recipe"]["name"] == "PDF Lasagna"


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


def test_garbled_font_pdf_routes_to_vision(client, monkeypatch):
    # A custom-font PDF whose letters extracted as mojibake reaches the endpoint
    # as plenty of text, but it is mostly unreadable glyphs (FoodAssistant-nw0k),
    # so it takes the same render-and-read-with-vision path a scan does
    # (FoodAssistant-k61s).
    import app.dependencies as deps
    import app.services.recipes_pdf as pdf
    garbled = "DanƑƒ's KiƆƨƌn' ChiƆƨƈn RƦƊƤiƈs " * 8
    vision = _FakeVisionProvider()
    monkeypatch.setattr(pdf, "extract_pdf_text", lambda raw, **k: garbled)
    monkeypatch.setattr(pdf, "render_pdf_pages", lambda raw, **k: [b"page1"])
    monkeypatch.setattr(deps, "get_vision_provider", lambda: vision)

    r = client.post("/mealie/recipes/import-pdf", files=_pdf())
    assert r.status_code == 200, r.text
    assert r.json()["recipe"]["name"] == "Scanned Stew"
    assert vision.calls == [b"page1"]


# -- Pure cleaning helpers (no pypdf, no network) --------------------------

def test_clean_collapses_whitespace():
    from app.services.recipes_pdf import _clean
    assert _clean("a  \t b\n\n\n c") == "a b\n c"


def test_clean_folds_ligatures_to_ascii():
    from app.services.recipes_pdf import clean_pdf_text
    # ﬁ ﬂ ﬀ ﬃ ligatures should come out as plain letters.
    assert clean_pdf_text("sauté the ﬁlets, ﬂour, oﬀ, ﬃll") \
        == "sauté the filets, flour, off, ffill"


def test_clean_strips_control_and_private_use():
    from app.services.recipes_pdf import clean_pdf_text
    # A private-use glyph and a NUL/control char become spaces and collapse away.
    assert clean_pdf_text("mix \x07flour\x00now") == "mix flour now"


def test_clean_nfkc_normalizes():
    from app.services.recipes_pdf import clean_pdf_text
    # A composed accent survives; a fullwidth digit is folded to ASCII.
    out = clean_pdf_text("bake ４０ min")
    assert out == "bake 40 min"


def test_is_mostly_garbage_flags_mojibake():
    from app.services.recipes_pdf import clean_pdf_text, is_mostly_garbage
    garbled = "DanƑƒ's KiƆƨƌn' ChiƆƨƈn RƦƊƤiƈs " * 4
    assert is_mostly_garbage(clean_pdf_text(garbled)) is True


def test_is_mostly_garbage_keeps_real_recipe():
    from app.services.recipes_pdf import clean_pdf_text, is_mostly_garbage
    real = ("Dan's Kitchen Chicken Recipes. Simmer the sauce for 20 minutes, "
            "then add jalapeño and a splash of crème fraîche. Serve hot.")
    assert is_mostly_garbage(clean_pdf_text(real)) is False


def test_is_mostly_garbage_ignores_short_text():
    from app.services.recipes_pdf import is_mostly_garbage
    # Too little to judge: leave it to the length gate, do not guess.
    assert is_mostly_garbage("Ƒƒ Ɔƨ") is False


# -- Page renderer (pypdfium2 mocked, no real render) ----------------------

def _fake_pdfium(num_pages=2):
    """A stand-in pypdfium2 module: PdfDocument yields pages whose render()
    hands back a tiny real PIL image, so render_pdf_pages produces PNG bytes
    without a bundled renderer or a real PDF."""
    from PIL import Image

    class _Bitmap:
        def to_pil(self):
            return Image.new("RGB", (6, 6), "white")

    class _Page:
        def render(self, scale=1.0):
            return _Bitmap()

    class _Doc:
        def __init__(self, raw):
            self._n = num_pages

        def __len__(self):
            return self._n

        def __getitem__(self, i):
            return _Page()

        def close(self):
            pass

    mod = types.ModuleType("pypdfium2")
    mod.PdfDocument = _Doc
    return mod


def test_render_pdf_pages_returns_png_bytes(monkeypatch):
    from app.services.recipes_pdf import render_pdf_pages
    monkeypatch.setitem(sys.modules, "pypdfium2", _fake_pdfium(num_pages=2))
    images = render_pdf_pages(b"%PDF-1.4 fake", max_pages=5)
    assert len(images) == 2
    for img in images:
        assert isinstance(img, bytes)
        assert img[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic number


def test_render_pdf_pages_caps_page_count(monkeypatch):
    from app.services.recipes_pdf import render_pdf_pages
    monkeypatch.setitem(sys.modules, "pypdfium2", _fake_pdfium(num_pages=10))
    images = render_pdf_pages(b"%PDF-1.4 fake", max_pages=3)
    assert len(images) == 3  # only the first max_pages are rendered


def test_render_pdf_pages_unreadable_raises_pdferror(monkeypatch):
    from app.services.recipes_pdf import PdfError, render_pdf_pages

    class _BadDoc:
        def __init__(self, raw):
            raise ValueError("not a pdf")

    mod = types.ModuleType("pypdfium2")
    mod.PdfDocument = _BadDoc
    monkeypatch.setitem(sys.modules, "pypdfium2", mod)
    with pytest.raises(PdfError):
        render_pdf_pages(b"garbage")


# -- Multi-page merge (pure) -----------------------------------------------

def test_merge_recipe_drafts_joins_pages_in_order():
    from app.services.recipes_pdf import merge_recipe_drafts
    merged = merge_recipe_drafts([
        {"name": "Stew", "servings": "6", "ingredients": ["beef"],
         "instructions": ["brown"]},
        {"name": "", "ingredients": ["broth", "beef"], "instructions": ["simmer"]},
    ])
    assert merged["name"] == "Stew"
    assert merged["servings"] == "6"
    # Page order preserved; the duplicate "beef" is dropped.
    assert merged["ingredients"] == ["beef", "broth"]
    assert merged["instructions"] == ["brown", "simmer"]


def test_merge_recipe_drafts_all_empty_is_none():
    from app.services.recipes_pdf import merge_recipe_drafts
    assert merge_recipe_drafts([None, {}, None]) is None
