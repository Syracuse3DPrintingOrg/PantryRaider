"""Pull readable text out of an uploaded recipe PDF.

The text is handed to the same LLM recipe extractor the URL-import fallback
uses, so a PDF import ends up in the same review-then-save flow as a webpage
import. Extraction is pure Python (pypdf, no poppler or other system deps).

A scanned or image-only PDF carries no extractable text; the caller detects
that from an empty (or too-short) result and shows a friendly message instead
of trying to guess.
"""
import re

# Sensible caps so a huge or hostile upload can't tie up a worker: most recipe
# PDFs are one or two pages, and the LLM extractor only needs a page or so of
# text to find the recipe.
MAX_PDF_BYTES = 15 * 1024 * 1024   # 15 MB
MAX_PDF_PAGES = 10
# Below this much extractable text we treat the PDF as image-only (a scan).
MIN_RECIPE_TEXT = 120


class PdfError(Exception):
    """Raised with a user-facing message when a PDF can't be read at all."""


def extract_pdf_text(raw: bytes, max_pages: int = MAX_PDF_PAGES) -> str:
    """Return the readable text of the first ``max_pages`` pages of a PDF.

    Raises PdfError (user-facing) when the bytes are not a readable PDF.
    Returns "" (or very little text) for a scanned / image-only PDF, which the
    caller treats as "no readable text".
    """
    from io import BytesIO

    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(BytesIO(raw))
    except (PdfReadError, ValueError, OSError, Exception) as e:  # noqa: BLE001
        raise PdfError("This file could not be read as a PDF.") from e

    parts: list[str] = []
    for page in reader.pages[:max_pages]:
        try:
            parts.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - a single bad page shouldn't sink the import
            continue
    return _clean(" \n".join(parts))


def _clean(text: str) -> str:
    """Collapse the whitespace pypdf leaves so the LLM sees tidy text."""
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()
