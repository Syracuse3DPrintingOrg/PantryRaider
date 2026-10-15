"""The managed AI proxy.

Gate order per request: instance token, task kind and text length (and, for
a batch, its line count), rate limit, entitlement and monthly quota (the
30-day trial or a paid plan), then forward. Over-quota answers 402 with a
structured body the app can surface exactly like its local token-budget gate
(service/app/routers/analyze.py). Image bytes pass through in memory only;
nothing image-shaped is ever persisted here.

A kind this service does not know is refused with 400 "Unknown task kind:
<kind>". The app reads that answer as "this Forager has no such kind yet" and
falls back to the kinds it already has (it and Forager deploy separately), so
keep the wording.
"""
from __future__ import annotations

import json

import anyio
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from .. import ratelimit, usage
from ..config import settings
from ..deps import (ACCOUNT_DISABLED_MESSAGE, current_instance, get_db,
                    utc_now_iso)
from ..forwarder import ForwarderError, get_forwarder
from ..models import Account, Instance
from ..uploads import UploadTooLarge, read_capped

router = APIRouter(prefix="/v1/ai", tags=["ai"])

# enrich_batch estimates shelf life and storage for a list of items in one
# call: the app sends a receipt's lines that way instead of one enrich call per
# line, which ran past the per-minute limit below on a long receipt.
_KINDS = {"food", "receipt", "enrich", "enrich_batch"}
_IMAGE_KINDS = {"food", "receipt"}
_ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/heic"}
_HEIC_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx",
                b"mif1", b"msf1"}


def image_bytes_match(data: bytes) -> bool:
    """Whether the upload's leading bytes are one of the accepted photo
    formats (JPEG, PNG, WebP, or HEIC/HEIF). Pure."""
    from ..recipe_images import sniff_image_ext
    if sniff_image_ext(data) in {"jpg", "png", "webp"}:
        return True
    return len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in _HEIC_BRANDS

# The most text one task may carry. The app sends a product's barcode data for
# enrichment, a couple of kilobytes at most, and a batch of receipt lines in
# well under this, so this leaves plenty of room while stopping a single call
# from pushing a book's worth of text upstream.
MAX_TEXT_CHARS = 8000
TEXT_TOO_LONG_MESSAGE = ("That request carries too much text. The limit is "
                         f"{MAX_TEXT_CHARS:,} characters.")
# The most items one enrich_batch task may carry. The forwarder's output cap
# for the kind is sized for a batch this long, so a longer one could come back
# cut off. The app splits a long receipt into batches of at most this many.
MAX_BATCH_ITEMS = 30
BATCH_TOO_LONG_MESSAGE = ("That request carries too many items. The limit is "
                          f"{MAX_BATCH_ITEMS} per request.")
BATCH_UNREADABLE_MESSAGE = "A batch request needs a JSON list of items."
# The largest photo the proxy reads (FoodAssistant-fa8). A phone photo is a
# few megabytes; reading stops at this cap instead of buffering whatever a
# client sends.
MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_TOO_LARGE_MESSAGE = "That photo is too large. The limit is 20 MB."
# The share of a reservation held for an attached photo. Text is estimated at
# about one token per three characters.
_IMAGE_RESERVATION_TOKENS = 1500


def batch_size(text: str) -> int | None:
    """How many items an enrich_batch request carries, or None when its text
    is not a non-empty JSON list of objects."""
    try:
        items = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if (not isinstance(items, list) or not items
            or not all(isinstance(item, dict) for item in items)):
        return None
    return len(items)


def reservation_estimate(text: str, has_image: bool) -> int:
    """The tokens to hold in the ledger before forwarding one task, sized from
    the request itself rather than a flat guess, and never below the
    configured floor (settings.proxy_reservation_tokens). It is reconciled to
    the real count once the provider answers, so it only has to be in the
    right range."""
    estimate = len(text) // 3 + (_IMAGE_RESERVATION_TOKENS if has_image else 0)
    return max(settings.proxy_reservation_tokens, estimate)


def _refuse_over_quota(state: dict) -> None:
    """Raise the 402 the app maps to its budget-gate message. Two ways to be
    refused: no active entitlement at all (the trial ran out and nothing paid
    replaced it; a Stripe plan, an admin comp, and the running trial all
    count via usage.has_active_access), or the active plan's monthly quota
    is spent (resolved in usage.quota_state)."""
    if not usage.has_active_access(state):
        raise HTTPException(402, detail={
            "error": "no_subscription",
            "plan": state["plan"],
            "used": state["used"],
            "quota": state["quota"],
            "month": state["month"],
            "message": "Your free trial has ended. Subscribe on the Forager "
                       "website to keep scanning, or switch to your own AI "
                       "key in Settings; scanning with your own key is "
                       "always free.",
        })
    if state["over_quota"]:
        raise HTTPException(402, detail={
            "error": "quota_exceeded",
            "plan": state["plan"],
            "used": state["used"],
            "quota": state["quota"],
            "month": state["month"],
            "message": "Monthly AI quota reached. It resets at the start of "
                       "next month.",
        })


def _admit(db: Session, inst: Instance, kind: str, text: str,
           has_image: bool) -> tuple[int, int]:
    """Every database gate before the upstream call, ending with the
    reservation committed. Returns (account_id, reservation_id) or raises the
    refusal. Runs in the threadpool (see analyze)."""
    account_id, instance_id = inst.account_id, inst.id
    owner = db.get(Account, account_id)
    if owner and owner.disabled:
        # Same admin kill switch as login and provisioning: a disabled
        # account's paired installs cannot spend either.
        db.rollback()
        raise HTTPException(403, detail={
            "error": "account_disabled",
            "message": ACCOUNT_DISABLED_MESSAGE,
        })
    # The service-wide daily ceiling, checked before any per-account work: a
    # clear 503 (logged at error level in usage.daily_ceiling_reached) rather
    # than an open-ended provider bill.
    if usage.daily_ceiling_reached(db):
        db.rollback()
        raise HTTPException(503, detail={
            "error": "daily_ceiling",
            "message": usage.DAILY_CEILING_MESSAGE,
        })
    # Reserve an estimated cost under a per-account lock BEFORE forwarding, so
    # a concurrent burst from an account with room for fewer requests than it
    # fires cannot all pass the gate before any usage lands. The reservation is
    # reconciled to the real token count on success and released on any failure
    # (see usage.gate_and_reserve), so it never becomes phantom usage. The
    # amount is sized from this request, so a long text or a photo holds more.
    state, reservation_id = usage.gate_and_reserve(
        db, account_id, instance_id, usage.month_key(),
        reservation_estimate(text, has_image), utc_now_iso())
    if reservation_id is None:
        _refuse_over_quota(state)
    return account_id, reservation_id


def _settle(db: Session, account_id: int, reservation_id: int, tokens: int,
            kind: str) -> dict:
    """Reconcile the reservation and read the quota the reply carries."""
    usage.reconcile_reservation(db, reservation_id, tokens, kind, utc_now_iso())
    state = usage.quota_state(db, account_id, usage.month_key())
    db.rollback()  # read-only from here: hand the connection back now
    return state


@router.post("/analyze")
async def analyze(
    kind: str = Form(...),
    text: str = Form(""),
    image: UploadFile | None = File(None),
    inst: Instance = Depends(current_instance),
    db: Session = Depends(get_db),
):
    """One AI task: gate, reserve, forward, settle.

    The handler is async so the upstream call (often seconds) holds no
    thread, but every database step runs in the threadpool. On the event loop
    a query that waited for a pooled connection blocked the loop itself, while
    the requests holding the connections needed that same loop to finish and
    give them back: past the pool's size in concurrent calls the whole service
    froze until the pool timed out. No connection is held across the upstream
    call either: the reservation is committed before it, and the settle step
    checks one out afresh."""
    if kind not in _KINDS:
        raise HTTPException(400, detail=f"Unknown task kind: {kind}")
    if len(text) > MAX_TEXT_CHARS:
        # Refused before the ledger or the provider sees it: nothing reserved,
        # nothing forwarded.
        raise HTTPException(413, detail=TEXT_TOO_LONG_MESSAGE)
    if kind == "enrich_batch":
        # Same: a batch that cannot be read, or that is too long for the
        # kind's output cap, never reaches the ledger or the provider.
        count = batch_size(text)
        if count is None:
            raise HTTPException(400, detail=BATCH_UNREADABLE_MESSAGE)
        if count > MAX_BATCH_ITEMS:
            raise HTTPException(413, detail=BATCH_TOO_LONG_MESSAGE)
    has_image = kind in _IMAGE_KINDS and image is not None
    # The instance id is read in the threadpool too: after the dependency's
    # commit the row is expired, and touching it here would query on the loop.
    instance_id = await run_in_threadpool(lambda: inst.id)
    if not ratelimit.allow(f"proxy:{instance_id}", settings.proxy_rate_per_minute):
        raise HTTPException(429, detail="Too many requests, slow down")
    account_id, reservation_id = await run_in_threadpool(
        _admit, db, inst, kind, text, has_image)

    image_data: bytes | None = None
    mime = ""
    try:
        if kind in _IMAGE_KINDS:
            if image is None:
                raise HTTPException(400, detail="Image tasks need an image upload")
            mime = image.content_type or ""
            if mime not in _ALLOWED_MIME:
                raise HTTPException(400, detail=f"Unsupported image type: {mime}")
            try:
                image_data = await read_capped(image, MAX_IMAGE_BYTES)
            except UploadTooLarge:
                raise HTTPException(413, detail=IMAGE_TOO_LARGE_MESSAGE)
            if not image_bytes_match(image_data):
                # The label alone is the client's word; bytes that are not a
                # photo would only be charged for an upstream error.
                raise HTTPException(400, detail="That file is not a photo "
                                                "Forager can read.")

        fwd = get_forwarder()
        try:
            result = await fwd.forward(kind, image_data, mime, text)
        except ForwarderError as exc:
            raise HTTPException(exc.status, detail=exc.detail)
        finally:
            del image_data  # transit only: the bytes never outlive the request
    except BaseException:
        # Any failure after the reservation (bad input, upstream error, cancel)
        # must give the reserved tokens back rather than leave phantom usage.
        # Shielded: in a cancelled scope every await is cancelled again, and
        # the release must still run.
        with anyio.CancelScope(shield=True):
            await run_in_threadpool(usage.release_reservation, db, reservation_id)
        raise

    # Shielded for the same reason: the tokens were spent, so the ledger has
    # to hear about it even if the caller hung up just now.
    with anyio.CancelScope(shield=True):
        state = await run_in_threadpool(_settle, db, account_id, reservation_id,
                                        result.tokens, kind)
    return {
        "result": result.result,
        "tokens": result.tokens,
        "quota": {"used": state["used"], "quota": state["quota"],
                  "remaining": state["remaining"], "month": state["month"],
                  "plan": state["plan"], "trial_days_left": state["trial_days_left"]},
    }
