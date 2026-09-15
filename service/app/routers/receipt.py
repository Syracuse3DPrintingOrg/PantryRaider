"""Receipt price capture endpoints (FoodAssistant-5osx).

POST /receipt/analyze accepts a receipt photo and answers immediately: the
vision-provider read takes a minute or two, so it runs as a background task
(the fast-ack pattern from routers/pending.py) and the user is free to leave
the page. The finished result persists to the receipt_results state file
(services/receipt_jobs.py), raises an action item in the Review inbox, and
fires a kiosk toast, so it is waiting wherever the user went. GET
/receipt/status is what the Shopping page polls while it is open; POST
/receipt/dismiss clears a stored result; POST /receipt/apply stamps each
confirmed price onto the matched product's newest stock entry and clears the
stored result too. The pure logic (prompt, parse, matching, newest-entry
pick) lives in services/receipt.py; the job state in services/receipt_jobs.py.
"""
import asyncio
import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import get_vision_provider
from ..services import action_items, ha_events
from ..services import receipt as receipt_service
from ..services import receipt_jobs
from ..services.grocy import GrocyClient, GrocyError
from .analyze import _ALLOWED_MIME, _MAX_DIM_RECEIPT, _check_budget, _downscale

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/receipt", tags=["receipt"])

_UNSUPPORTED_MSG = (
    "Receipt price capture is not available with the current AI setup. It "
    "works with a Gemini, OpenAI, Anthropic, or Ollama provider configured "
    "under Settings, AI."
)
_UNREADABLE_MSG = (
    "The receipt could not be read. The AI service may have returned an "
    "unexpected reply or be briefly unavailable. Try again in a moment."
)


class _JobFailed(Exception):
    """A background read failure with a user-forward message."""


def _retire_item(db: Session, state: dict | None) -> None:
    """Archive the inbox item attached to a cleared receipt slot, if any."""
    if state and state.get("action_item_id"):
        try:
            action_items.archive(db, int(state["action_item_id"]))
        except Exception:  # noqa: BLE001 - a stale inbox row never blocks the flow
            logger.exception("Could not archive the receipt action item")


@router.post("/analyze", status_code=202)
async def analyze_receipt_prices(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    provider=Depends(get_vision_provider),
):
    """Accept the photo, start the background read, and answer right away.

    Read-only: nothing is written to Grocy until /receipt/apply. A new upload
    supersedes whatever the slot held (a still-running read is cancelled, an
    unconfirmed result and its inbox item are retired).
    """
    if file.content_type not in _ALLOWED_MIME:
        raise HTTPException(400, f"Unsupported image type: {file.content_type}")
    _check_budget()
    data = await file.read()
    _retire_item(db, receipt_jobs.clear())
    job_id = receipt_jobs.start()
    task = asyncio.create_task(
        run_receipt_job(job_id, data, file.content_type, provider))
    receipt_jobs.register_task(job_id, task)
    return {"status": "reading", "message": receipt_jobs.READING_MESSAGE}


@router.get("/status")
async def receipt_status():
    """The stored receipt slot: none, pending, ready (with the proposed
    matches), or failed (with an honest message). The Shopping page polls
    this only while it is open."""
    state = receipt_jobs.get_status()
    if state.get("status") == receipt_jobs.STATUS_PENDING:
        state["message"] = receipt_jobs.READING_MESSAGE
    return state


@router.post("/dismiss")
async def dismiss_receipt(db: Session = Depends(get_db)):
    """Drop the stored result (or cancel a running read) and archive its
    Review inbox item."""
    _retire_item(db, receipt_jobs.clear())
    return {"ok": True}


async def _read_receipt(data: bytes, mime: str, provider) -> dict:
    """The actual read: downscale, provider, parse, match. Raises _JobFailed
    with a user-forward message on any expected failure."""
    small, small_mime = _downscale(data, mime, _MAX_DIM_RECEIPT)
    try:
        raw = await provider.extract_receipt_prices(small, small_mime)
    except HTTPException as e:
        # The provider already mapped its failure to a user-facing error
        # (budget/quota 429, unreachable-cloud 502): keep its message.
        detail = e.detail if isinstance(e.detail, str) else _UNREADABLE_MSG
        raise _JobFailed(detail)
    except Exception:
        logger.exception("Receipt price extraction failed")
        raise _JobFailed(_UNREADABLE_MSG)
    if raw is None:
        raise _JobFailed(_UNSUPPORTED_MSG)
    try:
        parsed = receipt_service.parse_receipt_reply(raw)
    except ValueError:
        logger.warning("Receipt price reply was not valid JSON")
        raise _JobFailed(_UNREADABLE_MSG)
    try:
        stock = await GrocyClient().get_stock()
    except GrocyError as e:
        raise _JobFailed(str(e))
    return {
        "store": parsed["store"],
        "items": receipt_service.match_line_items(parsed["items"], stock),
    }


def _receipt_payload() -> dict:
    """Inbox deep-link payload: the item links back to the Shopping page."""
    return {"url": "ui/shopping", "url_text": "Open the Shopping page"}


def _finish_ready(job_id: str, result: dict) -> None:
    """Persist the proposed matches, raise the inbox item, fire the toast."""
    from ..database import SessionLocal
    store, items = result["store"], result["items"]
    priced = receipt_jobs.priced_count(items)
    if priced:
        title = (f"Receipt read: {priced} price"
                 f"{'' if priced == 1 else 's'} ready to confirm")
        message = (f"{priced} price{' is' if priced == 1 else 's are'} ready "
                   "to confirm on the Shopping page.")
        level = "success"
    else:
        title = "Receipt read, nothing to confirm"
        message = ("No line on the receipt matched a pantry item with a "
                   "usable price. The Shopping page shows what was read.")
        level = "warning"
    body = (f"From {store}. " if store else "") + \
        "Open the Shopping page to review the pairings."
    db = SessionLocal()
    try:
        item = action_items.create(db, action_items.KIND_GENERIC, title,
                                   body=body, level=level,
                                   payload=_receipt_payload())
        if not receipt_jobs.mark_ready(job_id, store, items,
                                       action_item_id=item["id"]):
            # Superseded while finishing: drop the result, retire the item.
            action_items.archive(db, item["id"])
            return
    finally:
        db.close()
    try:
        ha_events.add_notification(message, title="Receipt read", level=level)
    except Exception:  # noqa: BLE001 - a toast failure never costs the result
        logger.exception("Receipt-read kiosk toast failed")


def _finish_failed(job_id: str, message: str) -> None:
    """Persist the honest failure and surface it once in the inbox too."""
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        item = action_items.create(
            db, action_items.KIND_GENERIC, "Receipt could not be read",
            body=f"{message} Send the photo again from the Shopping page.",
            level="warning", payload=_receipt_payload())
        if not receipt_jobs.mark_failed(job_id, message,
                                        action_item_id=item["id"]):
            action_items.archive(db, item["id"])
            return
    finally:
        db.close()
    try:
        ha_events.add_notification(message, title="Receipt", level="warning")
    except Exception:  # noqa: BLE001
        logger.exception("Receipt-failure kiosk toast failed")


async def run_receipt_job(job_id: str, data: bytes, mime: str, provider) -> None:
    """Background: read the receipt and persist the outcome.

    Runs after the upload was already acked. Uses its own database session
    (never a request session) and never raises: any failure lands in the
    state file as an honest, dismissable message. Cancellation (a newer
    upload or a dismiss took the slot) writes nothing.
    """
    message = None
    result = None
    try:
        result = await _read_receipt(data, mime, provider)
    except asyncio.CancelledError:
        raise
    except _JobFailed as e:
        message = str(e)
    except Exception:  # noqa: BLE001 - the background task must never crash the app
        logger.exception("Receipt reading failed")
        message = _UNREADABLE_MSG
    if result is not None:
        try:
            _finish_ready(job_id, result)
            return
        except Exception:  # noqa: BLE001
            logger.exception("Persisting the receipt result failed")
            message = _UNREADABLE_MSG
    try:
        _finish_failed(job_id, message)
    except Exception:  # noqa: BLE001 - even with the inbox down, never leave
        # the slot pending forever: persist the bare failure state.
        logger.exception("Recording the receipt failure failed")
        receipt_jobs.mark_failed(job_id, message)


class PricePair(BaseModel):
    """One user-confirmed pairing: this product's newest entry gets this price."""
    product_id: int = Field(gt=0)
    price: float
    name: str = ""


class ApplyRequest(BaseModel):
    pairs: list[PricePair]


@router.post("/apply")
async def apply_receipt_prices(body: ApplyRequest, db: Session = Depends(get_db)):
    """Write the confirmed prices into Grocy, one stock entry per pair.

    Failures are collected per item rather than aborting the batch, so one
    product with no stock left does not cost the rest of the receipt. Applying
    also clears the stored result and resolves its Review inbox item: the
    receipt has been handled.
    """
    grocy = GrocyClient()
    applied = 0
    failed: list[dict] = []
    for pair in body.pairs:
        label = pair.name.strip() or f"product {pair.product_id}"
        if not pair.price or pair.price <= 0:
            failed.append({"name": label,
                           "reason": "No price was read for this line."})
            continue
        try:
            entries = await grocy._get(
                f"/stock/products/{pair.product_id}/entries")
            entry = receipt_service.newest_entry(entries)
            if not entry:
                failed.append({
                    "name": label,
                    "reason": "No stock entry to price. Add the item to the "
                              "inventory first, then apply the receipt.",
                })
                continue
            await grocy.set_entry_price(entry, pair.price)
            applied += 1
        except GrocyError as e:
            failed.append({"name": label, "reason": str(e)})
        except Exception:
            logger.exception("Applying receipt price failed for %s", label)
            failed.append({"name": label,
                           "reason": "Unexpected error while writing the price."})
    state = receipt_jobs.clear()
    if state and state.get("action_item_id"):
        try:
            action_items.resolve(db, int(state["action_item_id"]))
        except Exception:  # noqa: BLE001
            logger.exception("Could not resolve the receipt action item")
    return {"applied": applied, "failed": failed}
