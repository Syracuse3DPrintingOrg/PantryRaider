"""Write lock for the shared JSON state files (FoodAssistant-k7cw, n6mta).

Several services share small state files under data_dir (timers, scanner mode,
current recipe, audit session, HA events, gadgets) using the same pattern:
atomic writes (temp file + os.replace) and mtime-cached reads. Writes being
atomic protects readers from torn files, but it never serialized two threads
doing a read-modify-write at the same time: both load the same snapshot, both
write, and one update is silently lost (two timers created from two requests
at once could eat each other, for example).

Pantry Raider runs as a single uvicorn worker, always (FoodAssistant-n6mta):
every launch path passes ``--workers 1``, and the instance guard warns when a
second process shares a data dir anyway. So the only concurrency left is
threads inside the one process (threadpool routes, background tasks), and this
lock is an in-process ``threading.RLock`` per state file, looked up in a
registry keyed by the file's resolved path. Every state-file module wraps only
its mutations with it; plain reads stay lock-free (the mtime cache already
makes them one stat call, and a reader can never be torn thanks to
os.replace). The mtime-cached reads still matter: a restore or a hand edit can
change a file underneath the running app.

No state file currently has a writer outside the app process that does a
read-modify-write while the app runs (firstboot seeds settings.json and
auth-state.json before the app starts, and a restore replaces files
wholesale), so none of them needs a cross-process lock. If one ever does, give
that file its own fcntl.flock on a sidecar and say why next to it.

The lock is reentrant, so nesting two ``state_write_lock`` blocks for the same
file in one call path is safe, and no sidecar ``.lock`` files are created.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path

_registry: dict[str, threading.RLock] = {}
_registry_lock = threading.Lock()


def _lock_for(state_file: Path) -> threading.RLock:
    """The one RLock for ``state_file``, created on first use.

    Keyed by the resolved path so two spellings of the same file (a relative
    data_dir, a symlink) share one lock.
    """
    try:
        key = str(Path(state_file).resolve())
    except (OSError, RuntimeError):
        key = os.path.abspath(str(state_file))
    with _registry_lock:
        lock = _registry.get(key)
        if lock is None:
            lock = _registry[key] = threading.RLock()
        return lock


@contextmanager
def state_write_lock(state_file: Path):
    """Hold the write lock for a read-modify-write of ``state_file``.

    Reentrant within a thread, exclusive across threads. It touches nothing on
    disk, so it works the same when data_dir is missing or unwritable.
    """
    with _lock_for(state_file):
        yield
