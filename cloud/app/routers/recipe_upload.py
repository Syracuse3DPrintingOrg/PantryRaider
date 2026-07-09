"""Share a recipe: the portal upload path for a signed-in member.

A member with a kitchen actively using Pantry Raider (or one an admin has
authorized) can add their own recipe here three ways: typing it in, uploading
a PDF, or uploading a photo. Whatever the way in, Forager's own AI key formats
it into a clean draft the member reviews and confirms before anything is saved.
The AI only formats and extracts; it never adds, removes, or changes an
ingredient, quantity, or technique.

A confirmed upload becomes a CommunityRecipe through the very same builder the
app's JSON submit uses, so a portal upload and an in-app share are the same row
and hit moderation the same way. Spam protection matches the rest of the write
paths: a required credit line, a hidden honeypot, a per-account and per-address
rate limit, and the human-check when it is enabled. Any trip stores nothing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import ratelimit, turnstile
from ..config import settings
from ..deps import (client_ip, cookie_account, get_db, is_admin, utc_now_iso)
from ..forwarder import ForwarderError, get_forwarder
from ..models import Account, Instance
from ..recipe_format import (NO_PDF_TEXT_MESSAGE, clean_text, extract_pdf_text,
                             format_recipe_draft)
from .recipes import can_upload, new_community_recipe, normalize_lines, \
    recipe_field_error

router = APIRouter(include_in_schema=False)

templates = Jinja2Templates(
    directory=str(Path(__file__).parent.parent / "templates"))

# The photo path accepts what the AI proxy accepts, so nothing unexpected is
# forwarded to the provider.
_ALLOWED_IMAGE_MIME = {"image/jpeg", "image/png", "image/webp", "image/heic"}
_PDF_MIME = "application/pdf"

GATE_MESSAGE = ("Recipe upload is for kitchens actively using Pantry Raider. "
                "Connect a kitchen, or ask us to enable uploads for your "
                "account.")
RATE_LIMITED = ("You are uploading recipes too quickly. Wait a minute and try "
                "again.")
AI_UNAVAILABLE = ("We could not format that recipe right now. Please try again "
                  "in a minute.")
NO_DRAFT = ("We could not read a recipe from what you sent. Check it has a "
            "title, ingredients, and steps, then try again.")
TOO_LARGE = "That file is too large. Please upload a recipe under 8 MB."
BAD_IMAGE = "Please upload a photo (JPG, PNG, WEBP, or HEIC)."
BAD_PDF = "Please upload a PDF file."
GENERIC_ERROR = "Something went wrong. Please try again."


def _account_instances(db: Session, account_id: int) -> list[Instance]:
    return db.query(Instance).filter_by(account_id=account_id).all()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


def _turnstile_site_key() -> str:
    return settings.turnstile_site_key if turnstile.enabled() else ""


def _render_upload(request: Request, account: Account, *, error: str = "",
                   notice: str = "", status_code: int = 200,
                   form: dict | None = None):
    return templates.TemplateResponse(request, "recipe_upload.html", {
        "signed_in": True,
        "is_admin": is_admin(account),
        "error": error,
        "notice": notice,
        "turnstile_site_key": _turnstile_site_key(),
        "form": form or {},
    }, status_code=status_code)


def _render_review(request: Request, account: Account, draft: dict,
                   attribution: str, *, error: str = "", status_code: int = 200):
    return templates.TemplateResponse(request, "recipe_upload_review.html", {
        "signed_in": True,
        "is_admin": is_admin(account),
        "error": error,
        "title": draft.get("title", ""),
        # The review form edits ingredients and steps as one-per-line text, the
        # same shape the confirm step normalizes back into a list.
        "ingredients_text": "\n".join(draft.get("ingredients", [])),
        "steps_text": "\n".join(draft.get("steps", [])),
        "attribution": attribution,
    }, status_code=status_code)


@router.get("/recipes/upload")
def upload_page(request: Request,
                account: Account | None = Depends(cookie_account),
                db: Session = Depends(get_db)):
    """The share-a-recipe page. Signed-in only; a member who cannot upload yet
    sees the friendly gate message instead of the form."""
    if not account:
        return _login_redirect()
    if not can_upload(account, _account_instances(db, account.id), _now()):
        return templates.TemplateResponse(request, "recipe_upload.html", {
            "signed_in": True,
            "is_admin": is_admin(account),
            "gate_blocked": True,
            "gate_message": GATE_MESSAGE,
        })
    notice = ("Thanks for sharing! Your recipe is in and headed to the "
              "community." if request.query_params.get("shared") else "")
    return _render_upload(request, account, notice=notice)


@router.post("/recipes/upload")
async def upload_submit(
        request: Request,
        method: str = Form("manual"),
        title: str = Form(""),
        ingredients: str = Form(""),
        steps: str = Form(""),
        attribution: str = Form(""),
        website: str = Form(""),
        cf_turnstile_response: str = Form("", alias="cf-turnstile-response"),
        pdf: UploadFile | None = File(None),
        photo: UploadFile | None = File(None),
        account: Account | None = Depends(cookie_account),
        db: Session = Depends(get_db)):
    """Take one uploaded recipe, run the spam and gate checks, then hand it to
    the AI for a formatted draft. Saves nothing: on success it renders the
    review page for the member to confirm."""
    if not account:
        return _login_redirect()

    kept = {"attribution": attribution}

    # Honeypot first: a bot that filled the hidden field learns nothing, and
    # nothing is forwarded or stored.
    if website.strip():
        return _render_upload(request, account, error=GENERIC_ERROR,
                              status_code=400, form=kept)

    # The browser path clears the same human-check the signup form uses.
    if not turnstile.verify(cf_turnstile_response, client_ip(request)):
        return _render_upload(request, account,
                              error="Please complete the challenge and try again.",
                              status_code=400, form=kept)

    # The gate: a real kitchen actively in use, or a hand-authorized account.
    if not can_upload(account, _account_instances(db, account.id), _now()):
        return templates.TemplateResponse(request, "recipe_upload.html", {
            "signed_in": True,
            "is_admin": is_admin(account),
            "gate_blocked": True,
            "gate_message": GATE_MESSAGE,
        }, status_code=403)

    # A credit line is required up front, before spending the AI key on a draft
    # a member cannot save anyway.
    if not attribution.strip():
        return _render_upload(request, account,
                              error="Please add a credit line saying who to thank "
                                    "or where this recipe came from.",
                              status_code=400, form=kept)

    # Per account and per address, so neither one account nor one network can
    # run up the AI bill or flood the library.
    limit = settings.recipe_upload_rate_per_minute
    if (not ratelimit.allow(f"recipe-upload-acct:{account.id}", limit)
            or not ratelimit.allow(f"recipe-upload-ip:{client_ip(request)}", limit)):
        return _render_upload(request, account, error=RATE_LIMITED,
                              status_code=429, form=kept)

    forwarder = get_forwarder()
    try:
        if method == "photo":
            draft, err = await _draft_from_photo(forwarder, photo)
        elif method == "pdf":
            draft, err = await _draft_from_pdf(forwarder, pdf)
        else:
            draft, err = await _draft_from_manual(forwarder, title,
                                                  ingredients, steps)
    except ForwarderError:
        return _render_upload(request, account, error=AI_UNAVAILABLE,
                              status_code=502, form=kept)

    if err:
        return _render_upload(request, account, error=err, status_code=400,
                              form={**kept, "title": title,
                                    "ingredients": ingredients, "steps": steps})
    if not draft or not (draft.get("ingredients") and draft.get("steps")):
        return _render_upload(request, account, error=NO_DRAFT,
                              status_code=400,
                              form={**kept, "title": title,
                                    "ingredients": ingredients, "steps": steps})

    return _render_review(request, account, draft, attribution)


async def _read_upload(upload: UploadFile | None) -> tuple[bytes, str]:
    """The bytes and error for an uploaded file, enforcing the size cap. Empty
    bytes with an error message when the upload is missing or too large."""
    if upload is None:
        return b"", ""
    data = await upload.read()
    if len(data) > settings.recipe_upload_max_bytes:
        return b"", TOO_LARGE
    return data, ""


async def _draft_from_manual(forwarder, title: str, ingredients: str,
                             steps: str) -> tuple[dict, str]:
    """Format typed entry. The member's own words are cleaned and sent through
    the AI for consistent formatting only; the review step keeps whatever they
    typed if the AI cannot improve it."""
    typed_lines = normalize_lines(ingredients) + normalize_lines(steps)
    if not title.strip() and not typed_lines:
        return {}, "Please type in your recipe: a title, ingredients, and steps."
    combined = clean_text(
        f"Title: {title.strip()}\n\nIngredients:\n{ingredients}\n\n"
        f"Steps:\n{steps}")
    draft = await format_recipe_draft(forwarder, text=combined)
    # Fall back to exactly what the member typed if the AI left a field blank,
    # so their content is never lost to a thin reply.
    if not draft.get("title"):
        draft["title"] = title.strip()[:200]
    if not draft.get("ingredients"):
        draft["ingredients"] = normalize_lines(ingredients)
    if not draft.get("steps"):
        draft["steps"] = normalize_lines(steps)
    return draft, ""


async def _draft_from_pdf(forwarder, pdf: UploadFile | None) -> tuple[dict, str]:
    """Format an uploaded PDF: read its text, then format it. A scan with no
    readable text gets the take-a-photo message rather than a wasted AI call."""
    if pdf is None or not (pdf.filename or "").strip():
        return {}, BAD_PDF
    if (pdf.content_type or "") not in (_PDF_MIME, "application/octet-stream"):
        return {}, BAD_PDF
    data, size_err = await _read_upload(pdf)
    if size_err:
        return {}, size_err
    text = extract_pdf_text(data)
    if not text:
        return {}, NO_PDF_TEXT_MESSAGE
    draft = await format_recipe_draft(forwarder, text=text)
    return draft, ""


async def _draft_from_photo(forwarder,
                            photo: UploadFile | None) -> tuple[dict, str]:
    """Format an uploaded photo through the vision path: the image goes to the
    AI, which reads and formats the recipe off the picture."""
    if photo is None or not (photo.filename or "").strip():
        return {}, BAD_IMAGE
    mime = (photo.content_type or "").lower()
    if mime not in _ALLOWED_IMAGE_MIME:
        return {}, BAD_IMAGE
    data, size_err = await _read_upload(photo)
    if size_err:
        return {}, size_err
    draft = await format_recipe_draft(forwarder, image_data=data, mime_type=mime)
    return draft, ""


@router.post("/recipes/upload/confirm")
def upload_confirm(request: Request,
                   title: str = Form(""),
                   ingredients: str = Form(""),
                   steps: str = Form(""),
                   attribution: str = Form(""),
                   website: str = Form(""),
                   account: Account | None = Depends(cookie_account),
                   db: Session = Depends(get_db)):
    """Save the reviewed draft as a CommunityRecipe, through the same builder
    the app's JSON submit uses. Re-checks the gate, the credit line, and the
    rate limit so nothing slips past by posting straight here."""
    if not account:
        return _login_redirect()

    draft = {
        "title": title.strip()[:200],
        "ingredients": normalize_lines(ingredients),
        "steps": normalize_lines(steps),
    }

    if website.strip():
        return _render_review(request, account, draft, attribution,
                              error=GENERIC_ERROR, status_code=400)

    if not can_upload(account, _account_instances(db, account.id), _now()):
        return templates.TemplateResponse(request, "recipe_upload.html", {
            "signed_in": True,
            "is_admin": is_admin(account),
            "gate_blocked": True,
            "gate_message": GATE_MESSAGE,
        }, status_code=403)

    limit = settings.recipe_upload_rate_per_minute
    if (not ratelimit.allow(f"recipe-upload-acct:{account.id}", limit)
            or not ratelimit.allow(f"recipe-upload-ip:{client_ip(request)}", limit)):
        return _render_review(request, account, draft, attribution,
                              error=RATE_LIMITED, status_code=429)

    error = recipe_field_error(draft["title"], draft["ingredients"],
                               draft["steps"], attribution)
    if error:
        return _render_review(request, account, draft, attribution,
                              error=error, status_code=400)

    now = utc_now_iso()
    recipe = new_community_recipe(
        title=draft["title"], description="", ingredients=draft["ingredients"],
        steps=draft["steps"], image_url="", attribution=attribution,
        submitter_account_id=account.id,
        require_approval=settings.recipe_require_approval, now=now)
    db.add(recipe)
    db.commit()
    return RedirectResponse("/recipes/upload?shared=1", status_code=303)
