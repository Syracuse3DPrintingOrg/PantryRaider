"""Scan-session presence flag (FoodAssistant-x61t).

A UART barcode scanner should read only while a scan page is actually open,
then read continuously until the user leaves. The open page reports itself
with a short heartbeat (POST /pending/scan-session/ping) every few seconds;
this module records the last heartbeat in a tiny state file under data_dir,
and the session counts as "active" while a heartbeat is recent (within
SESSION_TTL seconds). When the page is closed the pings stop and the session
expires on its own, so the host reader (which polls GET /gadgets/config) sees
scan_active fall to false and stops reading.

Same shared-state-file pattern as scanner_mode (FoodAssistant-3jxk): atomic
temp-file + os.replace writes under the cross-process lock (state_lock),
mtime-cached reads, and silent in-memory degradation when data_dir is not
writable (tests, a read-only mount). Every uvicorn worker then agrees on
whether a scan page is open, and the flag survives one worker restarting.

Local by design: a UART scanner is wired to one device, so "is a scan page
open here" is that device's own state and is never forwarded to a main server
(unlike the scanner mode, which is fleet-wide). A satellite that has its own
scan page and UART reader tracks its own session.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .state_lock import state_write_lock

# How long after the last heartbeat a session still counts as active. The scan
# page pings about every 10s, so 15s tolerates one dropped beat without
# flapping the scanner off between pings.
SESSION_TTL = 15.0

# The in-process view of the shared state file: the last heartbeat epoch and
# the file mtime it corresponds to, so a read can skip re-parsing an unchanged
# file. "mtime" is None until the file has been seen.
_state: dict = {"last_ping": 0.0, "mtime": None}


def _state_file() -> Path:
    # Resolved per call (not at import) so tests that repoint data_dir work.
    from ..config import settings
    return Path(settings.data_dir) / "scan_session.json"


def _load() -> None:
    """Refresh the in-process heartbeat from the state file if it changed."""
    try:
        sf = _state_file()
        mtime = sf.stat().st_mtime_ns
    except OSError:
        return  # no file yet (never pinged, or unwritable data_dir)
    if mtime == _state["mtime"]:
        return
    try:
        data = json.loads(sf.read_text())
    except (OSError, ValueError):
        return  # a torn or corrupt file never breaks a read; keep what we have
    _state["mtime"] = mtime
    try:
        _state["last_ping"] = float(data.get("last_ping") or 0.0)
    except (TypeError, ValueError):
        _state["last_ping"] = 0.0


def _save() -> None:
    """Write the current heartbeat (atomic replace, best effort)."""
    sf = _state_file()
    try:
        tmp = sf.with_name(sf.name + ".tmp")
        tmp.write_text(json.dumps({"last_ping": _state["last_ping"]}))
        os.replace(tmp, sf)
        _state["mtime"] = sf.stat().st_mtime_ns
    except OSError:
        pass  # data_dir not writable: fall back to process-local behavior


def ping(now: float | None = None) -> dict:
    """Record a heartbeat from an open scan page. Returns state()."""
    ts = time.time() if now is None else float(now)
    with state_write_lock(_state_file()):
        _state["last_ping"] = ts
        _save()
    return state(now=ts)


def is_active(now: float | None = None, ttl: float = SESSION_TTL) -> bool:
    """True when a scan page reported itself open within the last ttl seconds."""
    _load()
    ts = time.time() if now is None else float(now)
    return (ts - _state["last_ping"]) < ttl


def state(now: float | None = None) -> dict:
    """{active, expires_in}: whether a scan page is open, and how many more
    seconds the current heartbeat keeps the session alive (0 when inactive)."""
    _load()
    ts = time.time() if now is None else float(now)
    remaining = SESSION_TTL - (ts - _state["last_ping"])
    active = remaining > 0
    return {"active": bool(active),
            "expires_in": round(remaining, 1) if active else 0.0}


def reset() -> None:
    """Drop the session (an explicit stop, and used by tests)."""
    _state["last_ping"] = 0.0
    _state["mtime"] = None
    try:
        _state_file().unlink(missing_ok=True)
    except OSError:
        pass
