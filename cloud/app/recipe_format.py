"""Turn a member's uploaded recipe into a clean draft with Forager's AI key.

The portal upload path offers three ways in (typed text, a PDF, a photo) and
this module is the one place each becomes a draft the member reviews. The AI
is asked to format and extract only: never to add, remove, substitute, or
change any ingredient, quantity, or technique (see the "recipe" prompt in
forwarder.py). Nothing here saves anything; it returns a draft dict the router
renders for confirmation.

Everything is a small helper so the router stays thin and the logic is testable
with a mocked forwarder and in-memory PDF bytes, no network and no real
provider.
"""
from __future__ import annotations

import io
import json
import re

from .forwarder import AIForwarder

# The friendly message the router shows when a PDF has no extractable text (a
# scan or a page of images): the member is pointed at the photo path instead.
NO_PDF_TEXT_MESSAGE = ("We could not find any readable text in that PDF. If it "
                       "is a scan or a picture, try the photo option instead.")
# The soft cap on how much extracted or typed text is sent upstream, so a giant
# paste or PDF cannot run up the AI bill. Generous for any real recipe.
MAX_INPUT_CHARS = 20_000
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def clean_text(text: str) -> str:
    """Tidy raw text (a paste or PDF extraction) before it goes to the AI:
    normalize newlines, drop trailing spaces, and collapse long runs of blank
    lines, keeping paragraph breaks. Pure and length-capped."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    out: list[str] = []
    blanks = 0
    for line in lines:
        if line.strip():
            blanks = 0
            out.append(line)
        else:
            blanks += 1
            if blanks <= 1:
                out.append("")
    return "\n".join(out).strip()[:MAX_INPUT_CHARS]


def extract_pdf_text(data: bytes) -> str:
    """The readable text of a PDF, cleaned, or "" when there is none.

    A text PDF yields its words; a scanned or image-only PDF yields nothing,
    which the caller treats as "no readable text". Any parse trouble also reads
    as empty rather than raising, so a malformed upload fails soft."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        parts = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                # One bad page must not lose the rest of a readable document.
                continue
        return clean_text("\n".join(parts))
    except Exception:
        # A corrupt or encrypted upload reads as "no text" rather than raising,
        # so the upload path fails soft with the take-a-photo message.
        return ""


def parse_recipe_draft(raw: str) -> dict:
    """Turn the AI's reply into a draft dict {title, ingredients, steps}.

    Tolerant of a code fence around the JSON (the app hits the same thing) and
    of missing keys. Ingredients and steps come back as clean lists of strings;
    a reply that is not usable JSON yields empty lists, which the router reads
    as "could not read the recipe"."""
    from .routers.recipes import normalize_lines

    text = _FENCE_RE.sub("", (raw or "").strip())
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {"title": "", "ingredients": [], "steps": []}
    if not isinstance(data, dict):
        return {"title": "", "ingredients": [], "steps": []}
    return {
        "title": str(data.get("title") or "").strip()[:200],
        "ingredients": normalize_lines(data.get("ingredients")),
        "steps": normalize_lines(data.get("steps")),
    }


async def format_recipe_draft(forwarder: AIForwarder, *, text: str = "",
                              image_data: bytes | None = None,
                              mime_type: str = "") -> dict:
    """Ask the AI to format one uploaded recipe and return a review draft.

    Text (typed entry or extracted PDF) goes as the "recipe" task's text;
    a photo goes as its image. The forwarder owns the strict format-only
    prompt. Returns {title, ingredients, steps}; a ForwarderError from upstream
    propagates for the router to turn into a friendly, fail-soft message."""
    result = await forwarder.forward("recipe", image_data, mime_type,
                                     (text or "")[:MAX_INPUT_CHARS])
    return parse_recipe_draft((result.result or {}).get("text", ""))
