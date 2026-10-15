"""Bluetooth label printer (Supvan T50M family) setup status (FoodAssistant-h2j6).

Setting up the Supvan Bluetooth bridge runs on the host, through the bridge's
POST /print-setup with {"bluetooth": true} (scripts/image-build/
foodassistant-host-bridge, foodassistant-print-setup). That call can take
several minutes on first run, since it compiles a small Rust binary on the
device, so the router kicks it off as a background task and this module holds
the status a poll reads back: not_set_up, installing, ready, or failed.

Persisted to a tiny state file under data_dir, same shape as
services/scanner_mode.py and friends: a mtime-cached read so a shared data
dir keeps every uvicorn worker in agreement (the background task's worker may
not be the one a later poll lands on), atomic writes (temp file + os.replace),
and a silent in-memory-only fallback when data_dir is not writable.

Whether the printer-app service is REALLY running is not decided here: the
router asks the bridge for a live check (GET /print-setup/status) and
resolve_status() below combines that with the persisted outcome, because a
"successful" helper run does not by itself guarantee the Supvan bridge came
up (install_supvan_bridge warns rather than fails when neither its install
path succeeds).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

NOT_SET_UP = "not_set_up"
INSTALLING = "installing"
READY = "ready"
FAILED = "failed"

# A run that has been "installing" longer than this is presumed stalled (a
# bridge restart or crash lost the background task), so a poll reports it as
# failed rather than spinning forever. Comfortably above the ~20 minute
# on-device Rust build the bridge budgets for.
_STALE_INSTALL_SECONDS = 1800

# Keep the persisted log tail small; only the end of a long build log matters.
_LOG_TAIL_CHARS = 4000

_DEFAULT_STATE = {"status": NOT_SET_UP, "started_at": None, "log_tail": ""}

# The in-process view of the shared state file, plus the mtime it corresponds
# to so a read can skip re-parsing an unchanged file.
_state: dict = {**_DEFAULT_STATE, "mtime": None}


def _state_file() -> Path:
    # Resolved per call (not at import) so tests that repoint data_dir work.
    from ..config import settings
    return Path(settings.data_dir) / "bluetooth-print-setup.json"


def _load() -> None:
    """Refresh the in-process state from the state file if it changed on disk."""
    try:
        sf = _state_file()
        mtime = sf.stat().st_mtime_ns
    except OSError:
        return  # no file yet (never set up, or unwritable data_dir)
    if mtime == _state["mtime"]:
        return
    try:
        data = json.loads(sf.read_text())
    except (OSError, ValueError):
        return  # a torn or corrupt file never breaks a poll; keep what we have
    _state["mtime"] = mtime
    if isinstance(data, dict):
        _state["status"] = data.get("status") or NOT_SET_UP
        _state["started_at"] = data.get("started_at")
        _state["log_tail"] = data.get("log_tail") or ""


def _save() -> None:
    """Write the current state to the state file (atomic replace, best effort)."""
    sf = _state_file()
    try:
        sf.parent.mkdir(parents=True, exist_ok=True)
        tmp = sf.with_name(sf.name + ".tmp")
        tmp.write_text(json.dumps({
            "status": _state["status"],
            "started_at": _state["started_at"],
            "log_tail": _state["log_tail"],
        }))
        os.replace(tmp, sf)
        _state["mtime"] = sf.stat().st_mtime_ns
    except OSError:
        pass  # unwritable data_dir: state stays in-memory only for this worker


def current() -> dict:
    """The persisted state: {status, started_at, log_tail}."""
    _load()
    return {"status": _state["status"], "started_at": _state["started_at"],
            "log_tail": _state["log_tail"]}


def is_installing() -> bool:
    """Whether a setup run is already recorded as in flight."""
    return current()["status"] == INSTALLING


def mark_installing() -> None:
    """Record that a setup run has started, right before kicking it off."""
    _state["status"] = INSTALLING
    _state["started_at"] = time.time()
    _state["log_tail"] = ""
    _save()


def mark_result(ok: bool, log_tail: str) -> None:
    """Record the outcome of a finished setup run."""
    _state["status"] = READY if ok else FAILED
    _state["log_tail"] = (log_tail or "")[-_LOG_TAIL_CHARS:]
    _save()


def resolve_status(state: dict, live_active: Optional[bool],
                    now: Optional[float] = None) -> dict:
    """Combine the persisted state with a live bridge check into one status
    dict: {status, started_at?, log_tail?}.

    live_active is True/False when the bridge answered the GET /print-setup/
    status passthrough, or None when it could not be reached (a transient
    bridge hiccup should not flip a good "ready" to "failed"). Pure: no I/O,
    so it's unit-testable with plain dicts.
    """
    if live_active is True:
        out = {"status": READY}
        if state.get("log_tail"):
            out["log_tail"] = state["log_tail"]
        return out

    status = state.get("status") or NOT_SET_UP
    now = time.time() if now is None else now

    if status == INSTALLING:
        started = state.get("started_at")
        if started and (now - started) > _STALE_INSTALL_SECONDS:
            return {"status": FAILED, "log_tail":
                    "Setup did not finish in time. Try Set up again."}
        return {"status": INSTALLING, "started_at": started}

    if status == FAILED:
        return {"status": FAILED, "log_tail": state.get("log_tail", "")}

    if status == READY:
        if live_active is False:
            # The helper reported success at the time, but the printer
            # service is not live right now; be honest rather than sticky.
            return {"status": FAILED, "log_tail": state.get("log_tail", "") or
                    "Setup finished but the printer service is not running. "
                    "Try Set up again."}
        # live_active is None (bridge unreachable this poll): trust the
        # persisted outcome rather than downgrading on a transient hiccup.
        out = {"status": READY}
        if state.get("log_tail"):
            out["log_tail"] = state["log_tail"]
        return out

    return {"status": NOT_SET_UP}
