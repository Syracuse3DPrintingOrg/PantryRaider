"""JSON files that have to come back whole after the power goes off.

settings.json, the key store and the access state used to be written to a temp
file and renamed without a flush. os.replace keeps a reader from seeing half a
file, but on a box switched off at the wall the rename can reach the SD card
before the data, and the file comes back empty or full of NUL bytes. A loader
that read that as "no settings" then started the install with no password and
no keys, and the next save made it permanent.

write_json() and read_json() close that gap:

- A write goes to a mkstemp file in the same folder, is flushed and fsynced,
  replaces the target, and the folder is fsynced so the rename sticks. Then the
  file is read back, and only when it parses is a copy kept beside it as
  ``<name>.last-good`` (a separate file, so it does not share the new file's
  blocks on the card).
- A read of a file that is there but will not parse sets it aside as
  ``<name>.corrupt-<time>``, puts the last good copy back under the name and
  queues a notice for the inbox. With no good copy, the caller decides: the
  damaged file is either set aside (a store that may start empty) or left in
  place (settings, where starting empty would open the install up).
- A missing file is not damage: it reads as missing, so code that deletes a
  file to reset a store keeps working.

This module runs while config.py is still importing, so it must never import
the app. Notices wait in a list here until the app can store them
(action_items.flush_storage_notices).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger("foodassistant.durable")

LAST_GOOD_SUFFIX = ".last-good"
CORRUPT_MARK = ".corrupt-"

# Result states for read_json.
OK = "ok"                # parsed as it was
MISSING = "missing"      # no file: the store's own default applies
RECOVERED = "recovered"  # damaged; a good copy was put back
DAMAGED = "damaged"      # damaged and no good copy parsed
UNREADABLE = "unreadable"  # could not be opened (permissions, I/O): not damage


@dataclass
class Loaded:
    doc: Any = None
    state: str = MISSING
    mtime_ns: int | None = None
    raw: bytes | None = None
    set_aside: Path | None = None


def last_good_path(path) -> Path:
    path = Path(path)
    return path.with_name(path.name + LAST_GOOD_SUFFIX)


def is_side_copy(name: str) -> bool:
    """True for the copies this module keeps beside a file (.last-good and
    .corrupt-<time>). They describe this device's own history, so a backup
    never carries them and a restore never writes them."""
    return name.endswith(LAST_GOOD_SUFFIX) or CORRUPT_MARK in name


# --- Notices -----------------------------------------------------------------

_notice_lock = threading.Lock()
_notices: list[dict] = []


def record_notice(key: str, title: str, body: str, level: str = "warning") -> None:
    """Queue one inbox item. The same key replaces an earlier queued one."""
    with _notice_lock:
        _notices[:] = [n for n in _notices if n["key"] != key]
        _notices.append({"key": key, "title": title, "body": body, "level": level})


def pending_notices() -> list[dict]:
    with _notice_lock:
        return [dict(n) for n in _notices]


def take_notices() -> list[dict]:
    """Hand over every queued notice and clear the queue."""
    with _notice_lock:
        out = [dict(n) for n in _notices]
        _notices.clear()
        return out


def requeue_notices(items: Iterable[dict]) -> None:
    """Put notices back that could not be stored (the database was not ready)."""
    with _notice_lock:
        have = {n["key"] for n in _notices}
        for n in items:
            if n["key"] not in have:
                _notices.append(dict(n))


# --- Writing -----------------------------------------------------------------


def fsync_dir(folder: Path) -> None:
    if os.name == "nt":  # Windows cannot open a folder to flush it
        return
    try:
        fd = os.open(str(folder), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_bytes_atomic(path, data: bytes, *, mode: int = 0o600, sync: bool = True) -> int:
    """Write ``data`` to ``path`` so a power cut leaves the old file or the new
    one, never a mix. Returns the new file's st_mtime_ns. Raises OSError.

    sync=False keeps the private temp name and the atomic rename but skips both
    flushes, for a store written so often that flushing every save would wear
    the card more than losing the last save would cost. A power cut can then
    leave that file empty or damaged, so only state that may come back empty
    should pass it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp",
                               dir=str(path.parent))
    try:
        try:
            os.fchmod(fd, mode)
        except (AttributeError, OSError):
            pass
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            if sync:
                fh.flush()
                os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if sync:
        fsync_dir(path.parent)
    return path.stat().st_mtime_ns


def _refresh_last_good(path: Path, validate) -> None:
    """Copy the file just written to <name>.last-good, but only when what is
    on disk now reads back and parses. Best effort: the main write already
    stuck, so a failure here only costs the spare copy."""
    try:
        raw = path.read_bytes()
        if _parse(raw, validate) is _BAD:
            log.error("%s did not read back after a save; the last good copy was kept.",
                      path.name)
            return
        write_bytes_atomic(last_good_path(path), raw, mode=_mode_of(path))
    except OSError as exc:
        log.debug("could not refresh the last good copy of %s: %s", path.name, exc)


def _mode_of(path: Path) -> int:
    try:
        return path.stat().st_mode & 0o777
    except OSError:
        return 0o600


def write_json(path, doc, *, indent: int | None = None, sort_keys: bool = False,
               mode: int = 0o600, keep_last_good: bool = True,
               validate: Callable[[Any], bool] | None = None,
               sync: bool = True) -> int:
    """Serialize ``doc`` and write it durably. Returns st_mtime_ns.

    keep_last_good=False is for short-lived state (a five-minute pairing
    request) where a spare copy is not worth a second write. sync=False is for
    a hot store (see write_bytes_atomic). Raises OSError
    (and TypeError/ValueError for a document json cannot serialize, before
    anything on disk changes)."""
    path = Path(path)
    data = json.dumps(doc, indent=indent, sort_keys=sort_keys).encode("utf-8")
    mtime = write_bytes_atomic(path, data, mode=mode, sync=sync)
    if keep_last_good:
        _refresh_last_good(path, validate)
        # The copy is a second file in the same folder; the stat of the main
        # file is what callers cache, and the copy does not change it.
    return mtime


# --- Reading -----------------------------------------------------------------

_BAD = object()


def _parse(raw: bytes, validate):
    if not raw or not raw.strip() or not raw.strip(b"\x00 \t\r\n"):
        return _BAD
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _BAD
    check = validate if validate is not None else (lambda d: isinstance(d, dict))
    try:
        if not check(doc):
            return _BAD
    except Exception:  # noqa: BLE001 - a validator that trips counts as a bad file
        return _BAD
    return doc


def _set_aside(path: Path) -> Path | None:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    for n in range(0, 100):
        tail = f"{CORRUPT_MARK}{stamp}" + (f"-{n}" if n else "")
        dest = path.with_name(path.name + tail)
        if dest.exists():
            continue
        try:
            os.replace(path, dest)
        except OSError as exc:
            log.error("could not set the damaged %s aside: %s", path.name, exc)
            return None
        fsync_dir(path.parent)
        return dest
    return None


def read_json(path, *, validate: Callable[[Any], bool] | None = None,
              fallbacks: Iterable = (), what: str = "",
              keep_damaged_in_place: bool = False,
              damaged_title: str = "", damaged_body: str = "",
              notify: bool = True, notify_damaged: bool = True) -> Loaded:
    """Read a JSON file written by write_json, repairing it when it is damaged.

    validate(doc) -> bool says whether a parsed document has the right shape
    (default: a JSON object). ``fallbacks`` are older copies tried after
    <name>.last-good (settings.json.bak). ``what`` names the file for the
    owner in the inbox notice ("The settings file"). notify_damaged=False
    leaves the no-good-copy notice to a caller that raises its own. Never
    raises."""
    path = Path(path)
    try:
        st = path.stat()
    except FileNotFoundError:
        return Loaded(state=MISSING)
    except (OSError, ValueError):
        return Loaded(state=UNREADABLE)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return Loaded(state=MISSING)
    except OSError:
        return Loaded(state=UNREADABLE, mtime_ns=st.st_mtime_ns)
    doc = _parse(raw, validate)
    if doc is not _BAD:
        return Loaded(doc=doc, state=OK, mtime_ns=st.st_mtime_ns, raw=raw)

    # Damaged: look for a good copy before touching anything.
    good_raw = good_doc = None
    for cand in [last_good_path(path), *[Path(f) for f in fallbacks]]:
        try:
            craw = cand.read_bytes()
        except OSError:
            continue
        cdoc = _parse(craw, validate)
        if cdoc is not _BAD:
            good_raw, good_doc = craw, cdoc
            break
    label = what or path.name
    if good_doc is None:
        aside = None if keep_damaged_in_place else _set_aside(path)
        log.error("%s could not be read and no good copy was found%s.", path.name,
                  "" if keep_damaged_in_place else "; it was set aside")
        if notify and notify_damaged:
            kept = (f" The damaged file was kept as {aside.name} in the data folder."
                    if aside is not None else "")
            record_notice(
                f"storage_damaged:{path.name}:{aside.name if aside else st.st_mtime_ns}",
                damaged_title or f"{label} could not be read",
                (damaged_body or (f"{label} could not be read and no good copy was "
                                  "found, so it starts over empty.")) + kept,
                level="error")
        return Loaded(state=DAMAGED, mtime_ns=st.st_mtime_ns, raw=raw, set_aside=aside)

    aside = _set_aside(path)
    try:
        mtime = write_bytes_atomic(path, good_raw, mode=_mode_of(last_good_path(path)))
    except OSError as exc:
        # Cannot put it back on disk; still hand the good copy to the caller.
        log.error("could not put the last good copy of %s back: %s", path.name, exc)
        mtime = None
    log.error("%s was damaged and has been put back from its last good copy%s.",
              path.name, f" (the damaged file is {aside.name})" if aside else "")
    if notify:
        kept = (f" The damaged file was kept as {aside.name} in the data folder."
                if aside is not None else "")
        record_notice(
            f"storage_recovered:{path.name}:{aside.name if aside else time.time_ns()}",
            f"{label} was repaired",
            f"{label} could not be read, most likely because the power went off "
            "while it was being saved. The last good copy was put back, so the "
            "most recent change may be missing: check it and make that change "
            "again if needed." + kept,
            level="warning")
    return Loaded(doc=good_doc, state=RECOVERED, mtime_ns=mtime, raw=good_raw,
                  set_aside=aside)
