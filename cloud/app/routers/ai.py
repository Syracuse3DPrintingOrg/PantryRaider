"""The managed AI proxy.

Gate order per request: instance token, rate limit, entitlement and
monthly quota (the 30-day trial or a paid plan), then forward. Over-quota answers 402 with a structured body
the app can surface exactly like its local token-budget gate
(service/app/routers/analyze.py). Image bytes pass through in memory only;
nothing image-shaped is ever persisted here.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from .. import ratelimit, usage
from ..config import settings
from ..deps import (ACCOUNT_DISABLED_MESSAGE, current_instance, get_db,
                    utc_now_iso)
from ..forwarder import ForwarderError, get_forwarder
from ..models import Account, Instance

router = APIRouter(prefix="/v1/ai", tags=["ai"])

_KINDS = {"food", "receipt", "enrich"}
_ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/heic"}


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


@router.post("/analyze")
async def analyze(
    kind: str = Form(...),
    text: str = Form(""),
    image: UploadFile | None = File(None),
    inst: Instance = Depends(current_instance),
    db: Session = Depends(get_db),
):
    if kind not in _KINDS:
        raise HTTPException(400, detail=f"Unknown task kind: {kind}")
    if not ratelimit.allow(f"proxy:{inst.id}", settings.proxy_rate_per_minute):
        raise HTTPException(429, detail="Too many requests, slow down")
    owner = db.get(Account, inst.account_id)
    if owner and owner.disabled:
        # Same admin kill switch as login and provisioning: a disabled
        # account's paired installs cannot spend either.
        raise HTTPException(403, detail={
            "error": "account_disabled",
            "message": ACCOUNT_DISABLED_MESSAGE,
        })
    # Reserve an estimated cost under a per-account lock BEFORE forwarding, so
    # a concurrent burst from an account with room for fewer requests than it
    # fires cannot all pass the gate before any usage lands. The reservation is
    # reconciled to the real token count on success and released on any failure
    # (see usage.gate_and_reserve), so it never becomes phantom usage.
    state, reservation_id = usage.gate_and_reserve(
        db, inst.account_id, inst.id, usage.month_key(),
        settings.proxy_reservation_tokens, utc_now_iso())
    if reservation_id is None:
        _refuse_over_quota(state)

    image_data: bytes | None = None
    mime = ""
    try:
        if kind in ("food", "receipt"):
            if image is None:
                raise HTTPException(400, detail="Image tasks need an image upload")
            mime = image.content_type or ""
            if mime not in _ALLOWED_MIME:
                raise HTTPException(400, detail=f"Unsupported image type: {mime}")
            image_data = await image.read()

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
        usage.release_reservation(db, reservation_id)
        raise

    usage.reconcile_reservation(db, reservation_id, result.tokens, kind,
                                utc_now_iso())
    state = usage.quota_state(db, inst.account_id, usage.month_key())
    return {
        "result": result.result,
        "tokens": result.tokens,
        "quota": {"used": state["used"], "quota": state["quota"],
                  "remaining": state["remaining"], "month": state["month"],
                  "plan": state["plan"], "trial_days_left": state["trial_days_left"]},
    }
