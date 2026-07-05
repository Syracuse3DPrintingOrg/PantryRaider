"""Multi-process detection for a shared data_dir (FoodAssistant-0fho).

Uvicorn does not tell the app how many workers it was started with, so the app
cannot ask "am I one of several?". Instead each process claims a heartbeat
file, data_dir/app-instance.json, holding its pid and a timestamp that a
background task refreshes. On startup, if a DIFFERENT pid already holds a
fresh heartbeat and that pid is alive, this process logs a loud warning naming
the risk: two app processes are serving the same data dir.

This is belt-and-braces, not a refusal: the cross-worker state (timers, the
scanner mode, the audit session, the current recipe, HA events) is shared
through atomic state files, so multiple workers now agree on all of it. The
warning exists because anything process-local that slips in later (an lru
cache, a module dict) would silently disagree between workers, and because the
SQLite database is happier with one writer. Everything here is best-effort and
non-fatal: an unwritable data_dir simply disables the guard.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("foodassistant.instance")

# A heartbeat older than this is a dead claim (a crashed process, an unclean
# shutdown); refresh well inside the window so a live claim never looks stale.
FRESH_SECONDS = 60
HEARTBEAT_SECONDS = 20


def _guard_path() -> Path:
    from ..config import settings
    return Path(settings.data_dir) / "app-instance.json"


def _pid_alive(pid: int) -> bool:
    """True when ``pid`` is a live process we can see (signal 0 probe)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False


def other_live_instance(now: float | None = None) -> int | None:
    """The pid of a different live process holding a fresh heartbeat on this
    data dir, or None. Pure-ish (one file read, one signal-0 probe)."""
    if now is None:
        now = time.time()
    try:
        data = json.loads(_guard_path().read_text())
        pid = int(data.get("pid", 0))
        beat = float(data.get("updated", 0))
    except (OSError, ValueError, TypeError):
        return None
    if pid == os.getpid():
        return None
    if now - beat > FRESH_SECONDS:
        return None
    return pid if _pid_alive(pid) else None


def write_heartbeat() -> None:
    """Claim (or refresh) the heartbeat file for this process. Best-effort."""
    path = _guard_path()
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({
            "pid": os.getpid(),
            "updated": time.time(),
            "started": _STARTED,
        }))
        os.replace(tmp, path)
    except OSError:
        pass  # unwritable data_dir: the guard just stays silent


_STARTED = time.time()


def check_on_startup() -> int | None:
    """Warn loudly if another live app process shares this data dir, then claim
    the heartbeat for this process. Returns the other pid (for tests)."""
    other = other_live_instance()
    if other is not None:
        logger.warning(
            "MULTIPLE APP PROCESSES DETECTED: pid %s is also serving data dir "
            "%s (this is pid %s). This usually means uvicorn was started with "
            "several workers. Timers, scanner mode, the current recipe, the "
            "audit session, and on-screen events are shared through state "
            "files and stay consistent, but process-local caches (the Mealie "
            "recipe cache, external recipe search, the weather forecast, "
            "cached provider instances) and the SQLite database can disagree "
            "between workers. Run a single worker unless you know you need "
            "more.",
            other, _guard_path().parent, os.getpid(),
        )
    write_heartbeat()
    return other


async def heartbeat_task() -> None:
    """Keep this process's heartbeat fresh. Cancelled on shutdown."""
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        write_heartbeat()
