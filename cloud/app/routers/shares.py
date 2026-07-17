"""Recipe share links: send one recipe to one person by URL.

A share is the private counterpart to the community library: the recipe is
copied into its own row and addressed by an unguessable token, so only
someone holding the link can see it. The page at /r/{token} is public HTML
(the person receiving a recipe usually has no account), with a download
button that emits schema.org JSON-LD the app's importer reads directly.

Creating, listing, and revoking shares use the same two sign-in doors as
community recipes: a portal session or the app's linked instance credential.
The public page needs no sign-in at all; saving a share into your own
account and one-click reporting are the only things it offers a session.

Abuse control mirrors the community write path and adds one more layer:
share creation is rate limited per member and per address, and any share
that puts mail on the wire draws from a much slower per-hour budget, since
"email this link to anyone" is the textbook spam vector. Whether an email
address belongs to an account is never revealed: a share addressed to a
member lands in their inbox, one addressed to a stranger goes out by email,
and the response is identical either way.
"""
from __future__ import annotations

import html
import json
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pathlib import Path
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import ratelimit
from ..config import report_ip_pepper, settings
from ..deps import client_ip, cookie_account, get_db, is_admin, utc_now_iso
from ..email import base_url, send_email
from ..models import Account, SharedRecipe, SharedRecipeReport
from ..security import hash_ip
from .accounts import _valid_email
from .recipes import normalize_lines, recipe_field_error, require_actor

router = APIRouter()

templates = Jinja2Templates(
    directory=str(Path(__file__).parent.parent / "templates"))

# Validation caps, the same numbers the community library enforces.
MAX_TITLE = 200
MAX_DESCRIPTION = 4000
MAX_ITEMS = 100
MAX_ITEM_LEN = 500
MAX_MESSAGE = 500

RATE_LIMITED = "You are sharing recipes too quickly. Wait a minute and try again."
NOT_FOUND = "That share link could not be found."
# What the share email says the sender is. Accounts carry no display name,
# and putting the owner's email address in a stranger's inbox would leak it,
# so every share email speaks for an anonymous cook; the recipe's required
# attribution line carries the human credit.
SENDER_LABEL = "A Pantry Raider cook"


# --- Pure helpers (validation and shaping), unit-tested directly ------------

def image_url_ok(value: str) -> bool:
    """Whether the image link is empty or a plain PUBLIC web address.

    Anything else (javascript:, data:, a bare path) is refused: this string
    lands in an <img src> on a public page and in the JSON-LD download. Hosts
    that are IP literals or local names are refused too: every viewer's
    browser fetches this image, so an internal address (10.x, 127.x, a .local
    name) can never be a legitimate public photo, only a gadget for probing
    whatever network the viewer happens to be on."""
    if not value:
        return True
    if not (value.startswith("http://") or value.startswith("https://")):
        return False
    import ipaddress
    from urllib.parse import urlsplit
    host = (urlsplit(value).hostname or "").strip("[]").lower()
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        ipaddress.ip_address(host)
        return False  # any IP literal: public images live behind hostnames
    except ValueError:
        return True


def share_field_error(title: str, description: str, ingredients: list[str],
                      steps: list[str], attribution: object,
                      image_url: str) -> str:
    """The first thing wrong with a share about to be created, or "" when it
    is complete. Reuses the community completeness rules (title, at least one
    ingredient and step, a credit line) and adds the size caps: a share page
    is rendered for strangers, so runaway content is refused, not stored."""
    error = recipe_field_error(title, ingredients, steps, attribution)
    if error:
        return error
    if len(description) > MAX_DESCRIPTION:
        return "Please keep the description under 4000 characters."
    if len(ingredients) > MAX_ITEMS or len(steps) > MAX_ITEMS:
        return ("A shared recipe can have at most 100 ingredients and "
                "100 steps.")
    if any(len(item) > MAX_ITEM_LEN for item in ingredients + steps):
        return "Keep each ingredient and step under 500 characters."
    if not image_url_ok(image_url):
        return "The image link must be a web address starting with http or https."
    return ""


def share_summary(ingredients: list[str], limit: int = 5) -> str:
    """A short taste of the recipe for the share email: the first few
    ingredients, one per line."""
    lines = ingredients[:limit]
    if len(ingredients) > limit:
        lines.append("...and more")
    return "\n".join(f"- {line}" for line in lines)


def share_email_bodies(*, title: str, url: str, message: str,
                       ingredients: list[str]) -> tuple[str, str, str]:
    """The (subject, text, html) for a share email. Pure, so tests read the
    exact copy. Everything user-written is escaped in the HTML variant; the
    plain-text variant needs no escaping by nature."""
    subject = f"{SENDER_LABEL} shared a recipe with you: {title}"
    parts = [f"{SENDER_LABEL} shared a recipe with you: {title}."]
    if message:
        parts.append(f'They added a note:\n"{message}"')
    summary = share_summary(ingredients)
    if summary:
        parts.append(f"A taste of what goes in it:\n{summary}")
    parts.append(f"See the whole recipe here:\n{url}")
    parts.append("Shared through Pantry Raider (https://pantryraider.app), "
                 "the food tracker that is free and open source.")
    text = "\n\n".join(parts)

    esc_title = html.escape(title)
    html_parts = [f"<p>{SENDER_LABEL} shared a recipe with you: "
                  f"<strong>{esc_title}</strong>.</p>"]
    if message:
        html_parts.append(
            f"<p>They added a note:<br><em>{html.escape(message)}</em></p>")
    if ingredients:
        items = "".join(f"<li>{html.escape(i)}</li>" for i in ingredients[:5])
        more = "<li>...and more</li>" if len(ingredients) > 5 else ""
        html_parts.append(
            f"<p>A taste of what goes in it:</p><ul>{items}{more}</ul>")
    html_parts.append(
        f'<p><a href="{html.escape(url)}">See the whole recipe</a></p>')
    html_parts.append(
        '<p style="color:#888;font-size:small">Shared through '
        '<a href="https://pantryraider.app">Pantry Raider</a>, the food '
        'tracker that is free and open source.</p>')
    return subject, text, "\n".join(html_parts)


def share_json_ld(share: SharedRecipe) -> dict:
    """The schema.org Recipe document the download button hands out. This is
    the app importer's native food (recipes_import handles JSON-LD), so a
    downloaded share imports without any conversion step."""
    ingredients = json.loads(share.ingredients or "[]")
    steps = json.loads(share.steps or "[]")
    doc: dict = {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": share.title,
        "recipeIngredient": ingredients,
        "recipeInstructions": [{"@type": "HowToStep", "text": s}
                               for s in steps],
        "author": {"@type": "Person", "name": share.attribution},
    }
    if share.description:
        doc["description"] = share.description
    if share.image_url:
        doc["image"] = share.image_url
    return doc


def download_filename(title: str) -> str:
    """A safe attachment filename from the recipe title: ascii, dashes, no
    header-breaking characters."""
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:60]
    return f"{slug or 'recipe'}.json"


def og_description(description: str, ingredients: list[str]) -> str:
    """The social-preview blurb: the description when there is one, else the
    ingredient list, truncated to preview length."""
    text = (description or "").strip() or ", ".join(ingredients)
    text = " ".join(text.split())
    if len(text) > 160:
        text = text[:157].rstrip() + "..."
    return text


def share_page_payload(share: SharedRecipe) -> dict:
    """The full recipe payload for the recipient's inbox, ready to import."""
    return {
        "token": share.token,
        "title": share.title,
        "description": share.description,
        "ingredients": json.loads(share.ingredients or "[]"),
        "steps": json.loads(share.steps or "[]"),
        "image_url": share.image_url,
        "attribution": share.attribution,
        "created_at": share.created_at,
    }


# --- Request bodies ----------------------------------------------------------

class ShareSubmission(BaseModel):
    title: str = ""
    description: str = ""
    ingredients: list[str] | str = ""
    steps: list[str] | str = ""
    image_url: str = ""
    attribution: str = ""
    # An email address to aim the share at. If it belongs to a member the
    # share lands in their inbox; otherwise the link goes out by email. The
    # response never says which happened.
    recipient: str = ""
    # An email address to send the link to, member or not.
    email_to: str = ""
    # An optional personal note carried in the share email.
    message: str = ""


# --- Internal helpers --------------------------------------------------------

def _live_share(db: Session, token: str) -> SharedRecipe | None:
    """The share behind a token, or None when unknown or revoked. Revoked and
    never-existed are deliberately indistinguishable."""
    if not token:
        return None
    share = db.query(SharedRecipe).filter_by(token=token).first()
    if not share or share.revoked:
        return None
    return share


def _not_found_page(request: Request, viewer: Account | None) -> Response:
    return templates.TemplateResponse(request, "not_found.html", {
        "signed_in": viewer is not None,
        "is_admin": is_admin(viewer),
    }, status_code=404)


def _send_share_email(account: Account, share: SharedRecipe,
                      to: str, message: str) -> bool:
    """Send the share link to one address, spending the account's hourly
    email budget first (a failed send still spends it, so retry loops cannot
    hammer the mail server). Fail-soft like all Forager mail: a down mail
    server never fails the share, it just means no message arrived."""
    if not ratelimit.allow(f"share-email-acct:{account.id}",
                           settings.share_email_per_hour,
                           window_seconds=3600):
        return False
    url = f"{base_url()}/r/{share.token}"
    ingredients = json.loads(share.ingredients or "[]")
    subject, text, html_body = share_email_bodies(
        title=share.title, url=url, message=message, ingredients=ingredients)
    return send_email(to, subject, text, html_body)


# --- JSON API (dual auth: portal session or instance token) ------------------

@router.post("/v1/recipes/shares")
def create_share(payload: ShareSubmission, request: Request,
                 db: Session = Depends(get_db)):
    """Create a share link for one recipe. Free for every signed-in member,
    from the website or from the app."""
    account, _ = require_actor(request, db)

    # Rate limit per member and per address: share links are cheap to mint
    # and each one is a public page, so neither one account nor one network
    # gets to mass-produce them.
    limit = settings.share_create_rate_per_minute
    if (not ratelimit.allow(f"share-create-acct:{account.id}", limit)
            or not ratelimit.allow(f"share-create-ip:{client_ip(request)}", limit)):
        raise HTTPException(429, detail=RATE_LIMITED)

    title = payload.title.strip()[:MAX_TITLE]
    description = (payload.description or "").strip()
    ingredients = normalize_lines(payload.ingredients)
    steps = normalize_lines(payload.steps)
    image_url = (payload.image_url or "").strip()[:1024]
    error = share_field_error(title, description, ingredients, steps,
                              payload.attribution, image_url)
    if error:
        raise HTTPException(400, detail=error)

    recipient = (payload.recipient or "").strip().lower()
    email_to = (payload.email_to or "").strip().lower()
    message = (payload.message or "").strip()[:MAX_MESSAGE]
    if recipient and not _valid_email(recipient):
        raise HTTPException(400, detail="Enter a valid email address for the "
                                        "person you are sharing with.")
    if email_to and not _valid_email(email_to):
        raise HTTPException(400, detail="Enter a valid email address to send "
                                        "the link to.")

    # Addressed shares: an email that belongs to a member fills their inbox;
    # any other email gets the link by mail below. The response is identical
    # either way, so this endpoint cannot be used to test who has an account.
    recipient_account = None
    if recipient:
        recipient_account = db.query(Account).filter_by(email=recipient).first()

    now = utc_now_iso()
    share = SharedRecipe(
        token=secrets.token_urlsafe(24),
        owner_account_id=account.id,
        recipient_account_id=recipient_account.id if recipient_account else None,
        title=title, description=description,
        ingredients=json.dumps(ingredients), steps=json.dumps(steps),
        image_url=image_url, attribution=payload.attribution.strip()[:500],
        created_at=now, updated_at=now)
    db.add(share)
    db.commit()

    # Outgoing mail, fail-soft. Only the explicit email_to send is reflected
    # in the response; the recipient fallback (a recipient address with no
    # account behind it) must not change the response shape or values, or it
    # would reveal exactly what this endpoint promises never to.
    emailed = False
    if email_to:
        emailed = _send_share_email(account, share, email_to, message)
    if recipient and not recipient_account and recipient != email_to:
        _send_share_email(account, share, recipient, message)

    return {"ok": True, "token": share.token,
            "url": f"{base_url()}/r/{share.token}", "emailed": emailed}


@router.get("/v1/recipes/shares")
def list_shares(request: Request, db: Session = Depends(get_db)):
    """The shares this member created, newest first, for a revocation UI.
    Compact on purpose: the owner already has the recipe."""
    account, _ = require_actor(request, db)
    rows = (db.query(SharedRecipe).filter_by(owner_account_id=account.id)
            .order_by(SharedRecipe.id.desc()).all())
    return [{
        "token": r.token,
        "title": r.title,
        "created_at": r.created_at,
        "revoked": bool(r.revoked),
        "view_count": r.view_count,
        "recipient_set": r.recipient_account_id is not None,
    } for r in rows]


@router.get("/v1/recipes/shares/inbox")
def share_inbox(request: Request, db: Session = Depends(get_db)):
    """Shares addressed to this member and still live, as full recipes so the
    app can import one directly."""
    account, _ = require_actor(request, db)
    rows = (db.query(SharedRecipe)
            .filter_by(recipient_account_id=account.id, revoked=0)
            .order_by(SharedRecipe.id.desc()).all())
    return [share_page_payload(r) for r in rows]


@router.post("/v1/recipes/shares/{token}/revoke")
def revoke_share(token: str, request: Request, db: Session = Depends(get_db)):
    """Turn a share link off. Owner only; the page answers 404 from then on.
    A token that is not yours answers exactly like one that does not exist."""
    account, _ = require_actor(request, db)
    share = db.query(SharedRecipe).filter_by(
        token=token, owner_account_id=account.id).first()
    if not share:
        raise HTTPException(404, detail=NOT_FOUND)
    share.revoked = 1
    share.updated_at = utc_now_iso()
    db.commit()
    return {"ok": True, "revoked": True}


# --- The public share page (no auth) -----------------------------------------

@router.get("/r/{token}", include_in_schema=False)
def share_page(token: str, request: Request,
               viewer: Account | None = Depends(cookie_account),
               db: Session = Depends(get_db)):
    """The share itself: a public recipe page for whoever holds the link."""
    share = _live_share(db, token)
    if not share:
        return _not_found_page(request, viewer)
    # Best-effort view counter; a write hiccup must never hide the recipe.
    try:
        share.view_count = (share.view_count or 0) + 1
        db.commit()
    except Exception:  # noqa: BLE001 - the counter is cosmetic
        db.rollback()
    ingredients = json.loads(share.ingredients or "[]")
    steps = json.loads(share.steps or "[]")
    return templates.TemplateResponse(request, "share_view.html", {
        "signed_in": viewer is not None,
        "is_admin": is_admin(viewer),
        "title": share.title,
        "description": share.description,
        "attribution": share.attribution,
        "ingredients": ingredients,
        "steps": steps,
        "image_url": share.image_url,
        "token": share.token,
        "page_url": f"{base_url()}/r/{share.token}",
        "og_description": og_description(share.description, ingredients),
        "saved": bool(request.query_params.get("saved")),
        "reported": bool(request.query_params.get("reported")),
    })


@router.get("/r/{token}/download", include_in_schema=False)
def share_download(token: str, request: Request,
                   db: Session = Depends(get_db)):
    """The recipe as a schema.org JSON-LD file, ready to import into Pantry
    Raider (or anything else that reads schema.org recipes)."""
    share = _live_share(db, token)
    if not share:
        raise HTTPException(404, detail=NOT_FOUND)
    body = json.dumps(share_json_ld(share), indent=2)
    filename = download_filename(share.title)
    return Response(content=body, media_type="application/ld+json", headers={
        "Content-Disposition": f'attachment; filename="{filename}"'})


@router.post("/r/{token}/save", include_in_schema=False)
def share_save(token: str, request: Request,
               viewer: Account | None = Depends(cookie_account),
               db: Session = Depends(get_db)):
    """Save this share into the signed-in viewer's own "Shared with you" list:
    a copy addressed to them, so the original owner revoking the public link
    later does not touch what they saved. The copy keeps the owner and the
    attribution, so credit travels with the recipe."""
    if not viewer:
        return RedirectResponse("/login", status_code=303)
    share = _live_share(db, token)
    if not share:
        return _not_found_page(request, viewer)
    # Saving twice keeps one copy: a double-click or a re-visit is a no-op.
    existing = (db.query(SharedRecipe)
                .filter_by(recipient_account_id=viewer.id,
                           owner_account_id=share.owner_account_id,
                           title=share.title, revoked=0).first())
    if not existing:
        now = utc_now_iso()
        db.add(SharedRecipe(
            token=secrets.token_urlsafe(24),
            owner_account_id=share.owner_account_id,
            recipient_account_id=viewer.id,
            title=share.title, description=share.description,
            ingredients=share.ingredients, steps=share.steps,
            image_url=share.image_url, attribution=share.attribution,
            created_at=now, updated_at=now))
        db.commit()
    return RedirectResponse(f"/r/{token}?saved=1", status_code=303)


@router.post("/r/{token}/report", include_in_schema=False)
def share_report(token: str, request: Request,
                 viewer: Account | None = Depends(cookie_account),
                 db: Session = Depends(get_db)):
    """Flag a shared link for a look. One flag per member (or per address for
    an anonymous visitor); enough distinct reporters revoke the link on their
    own, the same reactive moderation the community library uses."""
    share = _live_share(db, token)
    if not share:
        return _not_found_page(request, viewer)

    # The page is public, so the reporter may be anonymous: rate limit per
    # address always, plus per account when one is signed in.
    limit = settings.recipe_report_rate_per_minute
    if not ratelimit.allow(f"share-report-ip:{client_ip(request)}", limit):
        raise HTTPException(429, detail="You are flagging too quickly. "
                                        "Wait a minute and try again.")
    if viewer and not ratelimit.allow(f"share-report-acct:{viewer.id}", limit):
        raise HTTPException(429, detail="You are flagging too quickly. "
                                        "Wait a minute and try again.")

    # An anonymous reporter is identified by a short peppered hash of their
    # address, never the raw IP: the same address still dedupes to the same
    # key, but the database holds nothing that names a visitor.
    reporter_key = (f"acct:{viewer.id}" if viewer
                    else f"ip:{hash_ip(client_ip(request), report_ip_pepper())}")[:120]
    already = (db.query(SharedRecipeReport)
               .filter_by(share_id=share.id, reporter_key=reporter_key).first())
    if not already:
        db.add(SharedRecipeReport(share_id=share.id,
                                  reporter_key=reporter_key,
                                  created_at=utc_now_iso()))
        try:
            db.flush()  # the unique constraint is the real guard for a race
        except IntegrityError:
            db.rollback()
            return RedirectResponse(f"/r/{token}?reported=1", status_code=303)
        distinct = (db.query(SharedRecipeReport)
                    .filter_by(share_id=share.id).count())
        share.report_count = distinct
        threshold = settings.recipe_report_hide_threshold
        if threshold and distinct >= threshold:
            # Enough separate people flagged it: the link goes dark on its
            # own, mirroring the community auto-hide.
            share.revoked = 1
        share.updated_at = utc_now_iso()
        db.commit()
    return RedirectResponse(f"/r/{token}?reported=1", status_code=303)


# --- Portal (cookie session) helpers for the account page ---------------------

@router.post("/account/shares/{token}/revoke", include_in_schema=False)
def portal_revoke_share(token: str, request: Request,
                        viewer: Account | None = Depends(cookie_account),
                        db: Session = Depends(get_db)):
    """The account page's revoke button: same rule as the API (owner only),
    carried on the portal cookie and answered with a redirect."""
    if not viewer:
        return RedirectResponse("/login", status_code=303)
    share = db.query(SharedRecipe).filter_by(
        token=token, owner_account_id=viewer.id).first()
    if share:
        share.revoked = 1
        share.updated_at = utc_now_iso()
        db.commit()
    return RedirectResponse("/account?m=share-revoked#shares", status_code=303)
