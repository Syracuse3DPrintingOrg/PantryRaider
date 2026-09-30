"""Receipt reading job state.

Reading a receipt photo with the vision provider takes a minute or two, so
POST /receipt/analyze answers immediately and the read runs as a background
task inside the app process (the fast-ack pattern from routers/pending.py).
This module is the shared state for that flow: one job slot persisted to
``data_dir/receipt_results.json`` using the state-file pattern (atomic temp
file + os.replace writes, mtime-cached reads, the shared flock from
state_lock.py around each mutation), so a finished result waits for the user
across page loads and navigation instead of living only in the request that
uploaded the photo.

Liveness is decided in-process: uvicorn runs a single worker in every
deployment mode, so a persisted "pending" job whose id this process is not
running was started by a process that died mid-read. Reading the status
converts it, once, to an honest failed state ("that reading was lost") rather
than leaving a forever-pending card.

Everything here is a pure state round-trip (no provider, no Grocy, no DB); the
orchestration that fills the slot lives in routers/receipt.py.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from .state_lock import state_write_lock

STATUS_NONE = "none"
STATUS_PENDING = "pending"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

# User-forward copy, shared by the analyze response and the status poll so the
# page says the same thing however it learns a read is running.
READING_MESSAGE = (
    "Reading your receipt. This takes a minute or two, so feel free to leave "
    "this page: the prices will be waiting here, and a note lands in your "
    "Review inbox when they are ready."
)

# Shown when a persisted pending job belongs to a process that is gone (the
# app restarted mid-read). Honest and dismissable, never forever-pending.
LOST_MESSAGE = (
    "The app restarted while your receipt was being read, so that reading was "
    "lost. Send the photo again when you are ready."
)

# Jobs this process is actually running: job id -> asyncio.Task (or None
# between start() and register_task()). A pending state on disk whose id is
# not here has no process behind it anymore.
_running: dict[str, object] = {}

# The in-process view of the state file: the last parsed payload and the file
# mtime it corresponds to, so a status poll costs one stat call.
_cache: dict = {"data": None, "mtime": None}


def _state_file() -> Path:
    # Resolved per call (not at import) so tests that repoint data_dir work.
    from ..config import settings
    return Path(settings.data_dir) / "receipt_results.json"


def _load() -> None:
    """Refresh the in-process view from the state file if it changed on disk."""
    try:
        sf = _state_file()
        mtime = sf.stat().st_mtime_ns
    except OSError:
        if _cache["mtime"] is not None:
            # The file existed before and is gone now: the slot was cleared.
            _cache["data"] = None
            _cache["mtime"] = None
        return
    if mtime == _cache["mtime"]:
        return
    try:
        data = json.loads(sf.read_text())
    except (OSError, ValueError):
        return  # a torn or corrupt file never breaks a poll; keep what we have
    _cache["mtime"] = mtime
    _cache["data"] = data if isinstance(data, dict) else None


def _save_locked(data: dict | None) -> None:
    """Persist ``data`` (or remove the file for None). Atomic, best effort:
    an unwritable data_dir degrades to process-local memory. Caller holds the
    cross-process write lock."""
    sf = _state_file()
    _cache["data"] = data
    try:
        if data is None:
            sf.unlink(missing_ok=True)
            _cache["mtime"] = None
            return
        tmp = sf.with_name(sf.name + ".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, sf)
        _cache["mtime"] = sf.stat().st_mtime_ns
    except OSError:
        _cache["mtime"] = None


def start() -> str:
    """Claim the job slot for a new read. Returns the job id.

    Any previous result in the slot is overwritten: one receipt at a time is
    the whole model, and the router clears the old slot (and its inbox item)
    before starting a new job.
    """
    job_id = uuid.uuid4().hex
    with state_write_lock(_state_file()):
        _save_locked({"status": STATUS_PENDING, "job_id": job_id,
                      "started": time.time()})
    _running[job_id] = None
    return job_id


def register_task(job_id: str, task) -> None:
    """Attach the asyncio task running ``job_id`` so clear() can cancel it."""
    if job_id in _running:
        _running[job_id] = task


def mark_ready(job_id: str, store, items: list[dict],
               action_item_id: int | None = None) -> bool:
    """Persist a finished read. Returns False when the job was superseded
    (a newer job or a dismiss took the slot), in which case the result is
    dropped and the caller should retire anything it created for it."""
    return _finish(job_id, {
        "status": STATUS_READY,
        "job_id": job_id,
        "store": store,
        "items": items or [],
        "priced": priced_count(items),
        "action_item_id": action_item_id,
        "finished": time.time(),
    })


def mark_failed(job_id: str, message: str,
                action_item_id: int | None = None) -> bool:
    """Persist an honest failure for the user to see once and dismiss."""
    return _finish(job_id, {
        "status": STATUS_FAILED,
        "job_id": job_id,
        "message": str(message),
        "action_item_id": action_item_id,
        "finished": time.time(),
    })


def _finish(job_id: str, data: dict) -> bool:
    with state_write_lock(_state_file()):
        _load()
        current = _cache["data"] or {}
        if current.get("job_id") != job_id:
            _running.pop(job_id, None)
            return False
        _save_locked(data)
    _running.pop(job_id, None)
    return True


def get_status() -> dict:
    """The current slot as a dict, always with a "status" key.

    Converts a pending job with no live task behind it (the app restarted
    mid-read) into a persisted failed state with LOST_MESSAGE, so the UI and
    the status endpoint never show a read that can no longer finish.
    """
    _load()
    data = _cache["data"]
    if not data:
        return {"status": STATUS_NONE}
    if data.get("status") == STATUS_PENDING and data.get("job_id") not in _running:
        with state_write_lock(_state_file()):
            _load()
            data = _cache["data"] or {}
            if (data.get("status") == STATUS_PENDING
                    and data.get("job_id") not in _running):
                data = {"status": STATUS_FAILED, "job_id": data.get("job_id"),
                        "message": LOST_MESSAGE, "action_item_id": None,
                        "finished": time.time()}
                _save_locked(data)
            elif not data:
                return {"status": STATUS_NONE}
    return dict(data)


def clear() -> dict | None:
    """Empty the slot (dismiss, apply, or a new upload superseding it).

    Cancels any task still running for the old slot and returns the previous
    payload so the caller can retire its inbox item. Returns None when the
    slot was already empty.
    """
    for task in list(_running.values()):
        try:
            if task is not None and not task.done():
                task.cancel()
        except Exception:
            pass
    _running.clear()
    with state_write_lock(_state_file()):
        _load()
        prev = _cache["data"]
        _save_locked(None)
    return dict(prev) if prev else None


def priced_count(items) -> int:
    """How many read lines are ready to confirm: a price was read AND a
    pantry product matched. Mirrors what the review card pre-checks."""
    return sum(1 for it in items or []
               if isinstance(it, dict) and it.get("product_id") and it.get("price"))


def reset() -> None:
    """Forget everything and drop the state file (used by tests)."""
    clear()
    _cache["data"] = None
    _cache["mtime"] = None
