"""Pull readable text out of an uploaded recipe PDF.

The text is handed to the same LLM recipe extractor the URL-import fallback
uses, so a PDF import ends up in the same review-then-save flow as a webpage
import. Extraction is pure Python (pypdf, no poppler or other system deps).

A scanned or image-only PDF carries no extractable text; the caller detects
that from an empty (or too-short) result and shows a friendly message instead
of trying to guess.
"""
import re
import unicodedata

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
    return clean_pdf_text(" \n".join(parts))


# Common typographic ligatures a PDF extractor leaves as single codepoints.
# NFKC folds most of these on its own, but we map them first so the result is
# plain ASCII even when a font used a mapping NFKC cannot see.
_LIGATURES = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl",
    "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "ft", "ﬆ": "st",
}
_LIGATURE_RE = re.compile("|".join(map(re.escape, _LIGATURES)))

# Typographic marks that are legitimate in real recipe text and should count as
# readable even though they sit above ASCII (smart quotes, dashes, ellipsis).
_READABLE_MARKS = "‘’“”–—…•"


def _strip_unreadable(text: str) -> str:
    """Replace control, format, surrogate, and private-use characters with a
    space. A subset-embedded PDF font can leave glyphs in the private-use area or
    as raw control codes; those carry no text and only confuse the LLM."""
    out: list[str] = []
    for ch in text:
        if ch in "\n\t":
            out.append(ch)
            continue
        # Unicode "C" categories: Cc control, Cf format, Cs surrogate,
        # Co private use, Cn unassigned. None of these are real letters.
        if unicodedata.category(ch)[0] == "C":
            out.append(" ")
            continue
        out.append(ch)
    return "".join(out)


def clean_pdf_text(text: str) -> str:
    """Turn raw pypdf output into tidy text for the LLM: fold ligatures, apply
    NFKC normalization, drop control/private-use glyphs, and collapse whitespace.

    Pure function of its input so it is easy to unit-test. It does NOT try to
    un-scramble a custom font's letter remapping (that is not recoverable); use
    is_mostly_garbage to detect that case and fall back to the photo path."""
    text = _LIGATURE_RE.sub(lambda m: _LIGATURES[m.group()], text)
    text = unicodedata.normalize("NFKC", text)
    text = _strip_unreadable(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


# Backwards-compatible alias for the earlier private helper name.
_clean = clean_pdf_text


def _is_readable(ch: str) -> bool:
    """True for characters that appear in ordinary Latin-script text: ASCII,
    Latin-1 accented letters and symbols, and a few typographic marks."""
    o = ord(ch)
    if o <= 0x7F:            # ASCII: letters, digits, punctuation, space
        return True
    if 0xA0 <= o <= 0xFF:    # Latin-1 Supplement: café, jalapeño, ° etc.
        return True
    return ch in _READABLE_MARKS


def is_mostly_garbage(text: str, threshold: float = 0.2) -> bool:
    """True when too much of the text is outside normal Latin script to be a real
    recipe, i.e. font-mangled mojibake rather than words.

    A custom or subset-embedded PDF font can map letters onto unrelated Unicode
    codepoints (Latin Extended, IPA, and the like), so extraction yields
    word-shaped runs that are unreadable. Those characters are valid letters, so
    cleaning keeps them; we judge the whole page instead. When more than
    ``threshold`` of the non-space characters are not ordinary Latin text, treat
    the PDF like a scan and send the user to the photo path. Accented European
    text stays readable, so a genuine Latin-script recipe is never flagged, and a
    short string is left to the length gate rather than guessed at."""
    visible = [ch for ch in text if not ch.isspace()]
    if len(visible) < 20:
        return False
    bad = sum(1 for ch in visible if not _is_readable(ch))
    return bad / len(visible) > threshold
