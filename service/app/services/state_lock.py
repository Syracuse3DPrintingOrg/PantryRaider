"""Cross-process lock for the shared JSON state files (FoodAssistant-k7cw).

Several services share small state files under data_dir (timers, scanner mode,
current recipe, audit session, HA events, gadgets) using the same pattern:
atomic writes (temp file + os.replace) and mtime-cached reads. Writes being
atomic protects readers from torn files, but it never serialized two workers
doing a read-modify-write at the same time: both load the same snapshot, both
write, and one update is silently lost (two timers created in different
workers could eat each other, for example).

This module is the one shared fix: an exclusive fcntl.flock on a sidecar
``<state file>.lock`` held around each mutation's read-modify-write. Every
state-file module wraps only its mutations with it; plain reads stay lock-free
(the mtime cache already makes them one stat call, and a reader can never be
torn thanks to os.replace).

Degradation matches the state files themselves: if the lock file cannot be
created or locked (data_dir unwritable, as in tests or on a read-only mount),
the caller simply proceeds unlocked, which is exactly the old in-memory
behavior where cross-process agreement is impossible anyway.

Do not nest two ``state_write_lock`` blocks for the same file in one call
path: flock is held per open file description, so a second acquisition in the
same process blocks forever. Modules keep an unlocked ``*_locked`` core and
take the lock once at the public entry point.
"""
from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def state_write_lock(state_file: Path):
    """Hold an exclusive cross-process lock for a mutation of ``state_file``.

    Locks a sidecar ``<name>.lock`` file next to the state file (never the
    state file itself: os.replace swaps the inode out from under a lock).
    Always yields; when the sidecar cannot be created or locked the caller
    proceeds unlocked, mirroring the state files' quiet in-memory fallback.
    """
    sf = Path(state_file)
    fd = None
    try:
        fd = os.open(str(sf.with_name(sf.name + ".lock")),
                     os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        # data_dir unwritable (or flock unsupported on this filesystem):
        # degrade to the unlocked behavior rather than breaking the caller.
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        fd = None
    try:
        yield
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
