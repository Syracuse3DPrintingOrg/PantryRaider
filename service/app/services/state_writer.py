"""Write a small state file from a thread when a request changes it.

The state files under data_dir (timers, scanner mode, ...) are written with a
temp file and os.replace. That is quick on an idle disk, but the rename waits
behind the filesystem journal, and on a busy SD card (or while settings or the
key store are being flushed) it can take seconds. Done inside a request on the
event loop, every other request waits with it: kiosk polls, the Stream Deck,
Home Assistant.

A module keeps its in-memory copy as the truth, hands the bytes to its
StateWriter, and skips reloading the file while ``busy()`` (the file on disk is
older than memory until the write lands). Asked on the event loop, the write
runs in a thread; asked anywhere else (tests, scripts, a threadpool route) it
runs inline, after any background write of the same file has finished, so the
newest content always lands last.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Callable


def on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def write_replace(path: Path, data: bytes | None, sync: bool = True) -> int | None:
    """A durable atomic write (private temp file, fsync, rename, folder fsync,
    see durable.py); the new st_mtime_ns, or None when it failed. The flushes
    are why this runs off the event loop. sync=False keeps the private temp
    name and the rename but skips the flushes (durable.write_bytes_atomic).
    data=None removes the file instead (a store that was cleared); that always
    reports None."""
    from .. import durable
    try:
        if data is None:
            Path(path).unlink(missing_ok=True)
            return None
        return durable.write_bytes_atomic(path, data, sync=sync)
    except OSError:
        return None


class StateWriter:
    """Writes the newest content of one state file, off the event loop."""

    def __init__(self, name: str):
        self._name = name
        self._cv = threading.Condition()
        self._pending: tuple | None = None
        self._running = False
        self._writing = False

    def busy(self) -> bool:
        """True while memory is ahead of the file."""
        with self._cv:
            return self._running or self._pending is not None

    def write(self, path: Path, data: bytes | None, done: Callable[[int | None], None],
              *, sync: bool = True) -> None:
        """Put ``data`` at ``path`` (None removes the file). ``done(mtime_ns or
        None)`` runs once it is
        in place; it must not take the caller's lock (the caller may be holding
        it while an inline write waits here). sync=False skips the flushes;
        when a newer write replaces a waiting one that asked for them, the
        newer one keeps them."""
        if not on_event_loop():
            with self._cv:
                self._pending = None          # superseded by this write
                while self._writing:
                    self._cv.wait()
                self._writing = True
            try:
                done(write_replace(path, data, sync))
            finally:
                with self._cv:
                    self._writing = False
                    self._cv.notify_all()
            return
        with self._cv:
            if self._pending is not None and self._pending[3]:
                sync = True                   # keep the flush a dropped write asked for
            self._pending = (path, data, done, sync)
            if self._running:
                return
            self._running = True
        try:
            threading.Thread(target=self._run, name=f"{self._name}-write",
                             daemon=True).start()
        except RuntimeError:  # no new threads (interpreter shutting down)
            with self._cv:
                self._running = False
                item, self._pending = self._pending, None
            if item is not None:
                item[2](write_replace(item[0], item[1], item[3]))

    def _run(self) -> None:
        while True:
            with self._cv:
                while self._writing:
                    self._cv.wait()
                item, self._pending = self._pending, None
                if item is None:
                    self._running = False
                    self._cv.notify_all()
                    return
                self._writing = True
            path, data, done, sync = item
            try:
                done(write_replace(path, data, sync))
            except Exception:  # noqa: BLE001 - a writer thread must not die loudly
                pass
            finally:
                with self._cv:
                    self._writing = False
                    self._cv.notify_all()

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Wait for the file to catch up with memory (tests, shutdown)."""
        with self._cv:
            return self._cv.wait_for(
                lambda: not self._running and not self._writing and self._pending is None,
                timeout)
