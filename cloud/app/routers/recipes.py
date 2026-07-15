"""Forager community recipes: share, browse, download, rate, and report.

Sharing and downloading are free: any signed-in member can do both, whether
they are signed in through the website or from the app itself. There is no
paid gate here; a spent trial does not stop anyone from sharing or saving a
community recipe.

Two sign-in paths reach these routes. A member browsing the website carries a
login session; the app carries its own linked credential. A small resolver
accepts whichever is present and hands back the member behind it, so the rest
of the file never has to care which door someone came through.

Spam protection stacks four cheap, independent layers on the write path:
a required credit line (attribution), a hidden honeypot field that only an
automated form-stuffer fills, a per-member and per-address rate limit, and,
for the website path, the same human-check the signup form uses. Any trip
answers with a plain, generic error and stores nothing.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import ratelimit, turnstile
from ..config import settings
from ..deps import (account_for_session, client_ip, get_db, utc_now_iso)
from ..models import Account, CommunityRecipe, Instance, RecipeRating, RecipeReport
from ..security import token_hash

router = APIRouter(prefix="/v1/recipes", tags=["recipes"])

# Statuses a member of the public may browse and download.
PUBLIC_STATUS = "approved"

SIGN_IN_MESSAGE = "Please sign in to continue."
GENERIC_ERROR = "Something went wrong. Please try again."
ATTRIBUTION_REQUIRED = ("Please add a credit line saying who to thank or where "
                        "this recipe came from.")
RATE_LIMITED = "You are sharing recipes too quickly. Wait a minute and try again."
UPLOAD_BLOCKED = "Recipe sharing is turned off for this account."
RATE_LIMITED_REPORT = ("You are flagging recipes too quickly. Wait a minute and "
                       "try again.")


# --- Pure helpers (validation and shaping), unit-tested directly ------------

def clamp_stars(stars: object) -> int:
    """A whole star count from 1 to 5. Anything outside is pulled to the
    nearest end; anything not a number becomes 1, so a rating is always valid."""
    try:
        value = int(stars)
    except (TypeError, ValueError):
        return 1
    return max(1, min(5, value))


def average_rating(rating_count: int, rating_sum: int) -> float:
    """The mean star rating, rounded to one decimal, or 0.0 with no ratings."""
    if not rating_count:
        return 0.0
    return round(rating_sum / rating_count, 1)


def normalize_lines(value: object) -> list[str]:
    """A clean list of non-empty strings from either a list or a block of text.

    Accepts a JSON-style list (what the app sends) or a single string with one
    item per line (what a simple form posts), trimming blanks either way."""
    if isinstance(value, str):
        items = value.splitlines()
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        items = []
    return [str(item).strip() for item in items if str(item).strip()]


def attribution_ok(value: object) -> bool:
    """Whether the credit line carries real text (not blank or whitespace)."""
    return bool(str(value or "").strip())


def recipe_field_error(title: str, ingredients: list[str], steps: list[str],
                       attribution: object) -> str:
    """The first thing wrong with a recipe about to be saved, or "" when it is
    complete. One place both the app's JSON submit and the portal upload check
    the same rules, so a recipe reaching moderation always has a title, at
    least one ingredient and step, and a credit line."""
    if not title:
        return "Please give your recipe a title."
    if not ingredients:
        return "Please list at least one ingredient."
    if not steps:
        return "Please add at least one step."
    if not attribution_ok(attribution):
        return ATTRIBUTION_REQUIRED
    return ""


def new_community_recipe(*, title: str, description: str,
                         ingredients: list[str], steps: list[str],
                         image_url: str, attribution: str,
                         submitter_account_id: int, require_approval: bool,
                         now: str) -> CommunityRecipe:
    """Build a CommunityRecipe row from validated fields, applying the same
    trimming, length caps, and moderation-mode status choice for every source
    (the app's JSON submit and the portal upload). Where a new recipe lands is
    the recipe_require_approval choice: "pending" when a moderator must approve
    it first, "approved" when the library auto-approves and relies on reports
    plus the admin panel after the fact."""
    status = "pending" if require_approval else "approved"
    return CommunityRecipe(
        title=title.strip()[:200],
        description=(description or "").strip(),
        ingredients=json.dumps(ingredients),
        steps=json.dumps(steps),
        image_url=(image_url or "").strip()[:1024],
        attribution=attribution.strip()[:500],
        submitter_account_id=submitter_account_id,
        status=status,
        created_at=now,
        updated_at=now,
    )


def can_upload(account: Account | None, instances) -> bool:
    """Whether this account may upload its own recipes from the portal.

    Pure and testable. Automatic for any account with a linked kitchen that
    has checked in at least once (an Instance row with a last_seen_at): a
    verified Pantry Raider install vouches for its owner. The manual
    authorization flag (set by an admin) covers trusted contributors with no
    kitchen. The admin block flag wins over both, so revoking after abuse
    sticks even while a kitchen keeps checking in. A disabled or signed-out
    account is always refused."""
    if account is None or getattr(account, "disabled", 0):
        return False
    if getattr(account, "recipe_upload_blocked", 0):
        return False
    if getattr(account, "recipe_upload_authorized", 0):
        return True
    return any((getattr(inst, "last_seen_at", "") or "").strip()
               for inst in instances)


def recipe_card(recipe: CommunityRecipe) -> dict:
    """The compact shape a browse listing shows. report_count is deliberately
    left off: how often a recipe has been flagged is not public."""
    return {
        "id": recipe.id,
        "title": recipe.title,
        "description": recipe.description,
        "image_url": recipe.image_url,
        "attribution": recipe.attribution,
        "average_rating": average_rating(recipe.rating_count, recipe.rating_sum),
        "rating_count": recipe.rating_count,
    }


def recipe_full(recipe: CommunityRecipe) -> dict:
    """The complete recipe for saving a copy, ingredients and steps included."""
    card = recipe_card(recipe)
    card.update({
        "ingredients": json.loads(recipe.ingredients or "[]"),
        "steps": json.loads(recipe.steps or "[]"),
        "created_at": recipe.created_at,
    })
    return card


# --- Who is acting -----------------------------------------------------------

def _bearer(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    return ""


def resolve_actor(request: Request, db: Session) -> tuple[Account | None, str]:
    """The signed-in member behind the request, and which door they used.

    Returns (account, "session") for a website login, (account, "instance")
    for the app's linked credential, or (None, "") when neither is present or
    valid. A disabled account resolves to nobody."""
    token = _bearer(request)
    if not token:
        return None, ""
    account = account_for_session(db, token)
    if account:
        return account, "session"
    inst = db.query(Instance).filter_by(token_hash=token_hash(token)).first()
    if inst:
        owner = db.get(Account, inst.account_id)
        if owner and not owner.disabled:
            return owner, "instance"
    return None, ""


def require_actor(request: Request, db: Session) -> tuple[Account, str]:
    account, via = resolve_actor(request, db)
    if not account:
        raise HTTPException(401, detail=SIGN_IN_MESSAGE)
    return account, via


# --- Request bodies ----------------------------------------------------------

class RecipeSubmission(BaseModel):
    title: str = ""
    description: str = ""
    ingredients: list[str] | str = ""
    steps: list[str] | str = ""
    image_url: str = ""
    attribution: str = ""
    # A hidden field a person never sees or fills; only an automated form
    # stuffer trips it. Named to look ordinary to a bot.
    website: str = ""
    # The website's human-check response, verified only on the website path.
    turnstile_token: str = ""


class RatingBody(BaseModel):
    stars: int = 0


class ReportBody(BaseModel):
    reason: str = ""


# --- Endpoints ---------------------------------------------------------------

@router.post("")
def submit_recipe(payload: RecipeSubmission, request: Request,
                  db: Session = Depends(get_db)):
    """Share a recipe with the community. Requires being signed in; free for
    everyone, no plan needed. The one exception is an account an admin has
    blocked from sharing after abuse: the block covers this path too, not
    just the portal upload form."""
    account, via = require_actor(request, db)

    if getattr(account, "recipe_upload_blocked", 0):
        raise HTTPException(403, detail=UPLOAD_BLOCKED)

    # Honeypot first: a bot that filled the hidden field learns nothing, and
    # nothing is stored.
    if payload.website.strip():
        raise HTTPException(400, detail=GENERIC_ERROR)

    # The website path also clears the same human-check the signup form uses.
    # The app path (a linked install) skips it: it already proved itself when
    # it signed in.
    if via == "session" and not turnstile.verify(payload.turnstile_token,
                                                  client_ip(request)):
        raise HTTPException(400, detail="Please complete the challenge and try again.")

    # Rate limit per member and per address, so neither one account nor one
    # network can flood the shared library.
    limit = settings.recipe_submit_rate_per_minute
    if (not ratelimit.allow(f"recipe-submit-acct:{account.id}", limit)
            or not ratelimit.allow(f"recipe-submit-ip:{client_ip(request)}", limit)):
        raise HTTPException(429, detail=RATE_LIMITED)

    title = payload.title.strip()[:200]
    ingredients = normalize_lines(payload.ingredients)
    steps = normalize_lines(payload.steps)
    error = recipe_field_error(title, ingredients, steps, payload.attribution)
    if error:
        raise HTTPException(400, detail=error)

    now = utc_now_iso()
    recipe = new_community_recipe(
        title=title, description=payload.description,
        ingredients=ingredients, steps=steps, image_url=payload.image_url,
        attribution=payload.attribution, submitter_account_id=account.id,
        require_approval=settings.recipe_require_approval, now=now)
    db.add(recipe)
    db.commit()
    return {"id": recipe.id}


@router.get("")
def list_recipes(request: Request, q: str = "", page: int = 1,
                 per_page: int = 20, db: Session = Depends(get_db)):
    """Browse and search shared recipes. Open to everyone; only recipes that
    are visible to the community come back."""
    page = max(1, page)
    per_page = max(1, min(100, per_page))
    query = db.query(CommunityRecipe).filter_by(status=PUBLIC_STATUS)
    term = q.strip()
    if term:
        like = f"%{term}%"
        query = query.filter(
            CommunityRecipe.title.ilike(like)
            | CommunityRecipe.ingredients.ilike(like))
    total = query.count()
    rows = (query.order_by(CommunityRecipe.created_at.desc())
            .offset((page - 1) * per_page).limit(per_page).all())
    return {
        "recipes": [recipe_card(r) for r in rows],
        "page": page,
        "per_page": per_page,
        "total": total,
    }


@router.get("/{recipe_id}")
def get_recipe(recipe_id: int, request: Request, db: Session = Depends(get_db)):
    """The full recipe, ready to save a copy. Anyone may fetch a recipe that is
    visible to the community; the person who shared one can also fetch their
    own while it is still waiting on a review."""
    recipe = db.get(CommunityRecipe, recipe_id)
    if not recipe:
        raise HTTPException(404, detail="That recipe could not be found.")
    if recipe.status != PUBLIC_STATUS:
        actor, _ = resolve_actor(request, db)
        if not actor or actor.id != recipe.submitter_account_id:
            raise HTTPException(404, detail="That recipe could not be found.")
    return recipe_full(recipe)


@router.post("/{recipe_id}/rating")
def rate_recipe(recipe_id: int, payload: RatingBody, request: Request,
                db: Session = Depends(get_db)):
    """Rate a recipe from one to five stars. Rating again replaces your earlier
    rating rather than adding a second one."""
    account, _ = require_actor(request, db)
    recipe = db.get(CommunityRecipe, recipe_id)
    if not recipe or recipe.status != PUBLIC_STATUS:
        raise HTTPException(404, detail="That recipe could not be found.")
    stars = clamp_stars(payload.stars)
    existing = (db.query(RecipeRating)
                .filter_by(recipe_id=recipe_id, account_id=account.id).first())
    if existing:
        # Swap this member's earlier vote for the new one: adjust the running
        # sum by the difference, the count is unchanged.
        recipe.rating_sum += stars - existing.stars
        existing.stars = stars
    else:
        db.add(RecipeRating(recipe_id=recipe_id, account_id=account.id,
                            stars=stars, created_at=utc_now_iso()))
        recipe.rating_count += 1
        recipe.rating_sum += stars
    recipe.updated_at = utc_now_iso()
    db.commit()
    return {
        "average_rating": average_rating(recipe.rating_count, recipe.rating_sum),
        "rating_count": recipe.rating_count,
        "your_rating": stars,
    }


@router.post("/{recipe_id}/report")
def report_recipe(recipe_id: int, payload: ReportBody, request: Request,
                  db: Session = Depends(get_db)):
    """Flag a recipe for a look. Enough separate members flagging it pull it
    from the browser on its own until someone can review it.

    One flag per member: reporting the same recipe again is a quiet no-op, so
    no single account can loop reports to bury a recipe. The auto-hide counts
    distinct reporters, not raw calls."""
    account, _ = require_actor(request, db)
    recipe = db.get(CommunityRecipe, recipe_id)
    if not recipe:
        raise HTTPException(404, detail="That recipe could not be found.")

    # Rate limit per member and per address, so neither one account nor one
    # network can fire off a flood of flags.
    limit = settings.recipe_report_rate_per_minute
    if (not ratelimit.allow(f"recipe-report-acct:{account.id}", limit)
            or not ratelimit.allow(f"recipe-report-ip:{client_ip(request)}", limit)):
        raise HTTPException(429, detail=RATE_LIMITED_REPORT)

    # Already flagged by this member: nothing to record and nothing changes.
    existing = (db.query(RecipeReport)
                .filter_by(recipe_id=recipe_id, account_id=account.id).first())
    if existing:
        return {"reported": True}

    db.add(RecipeReport(recipe_id=recipe_id, account_id=account.id,
                        reason=payload.reason.strip()[:500],
                        created_at=utc_now_iso()))
    try:
        db.flush()  # the unique constraint is the real guard against a race
    except IntegrityError:
        db.rollback()
        return {"reported": True}

    # Recompute the tally from distinct reporters (one row per member now) and
    # decide the auto-hide from that count, never from raw report calls.
    distinct_reporters = (db.query(RecipeReport.account_id)
                          .filter_by(recipe_id=recipe_id).distinct().count())
    recipe.report_count = distinct_reporters
    threshold = settings.recipe_report_hide_threshold
    if (threshold and distinct_reporters >= threshold
            and recipe.status == "approved"):
        recipe.status = "hidden"
    recipe.updated_at = utc_now_iso()
    db.commit()
    return {"reported": True}
