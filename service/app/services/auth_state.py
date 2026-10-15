"""Device-local access state: data_dir/auth-state.json.

What this install knows about its own access history, kept in one small JSON
file (the state-file pattern: the state_lock write lock around each change, temp file plus os.replace, 0600 from the first byte,
mtime-cached reads, quiet in-memory fallback when data_dir is unwritable):

- origin: 'fresh' (first started on 0.21 or later) or 'upgraded'. Upgraded
  installs keep the old trust for programs on this device and a 14-day grace
  for visitors through a proxy on an install with no password.
- loopback_trust: whether programs on this device still get in without a key
  (legacy), and when that ended.
- legacy_use, agents, keyless callers, sightings: counters the retirement
  rule, the Security pane and the inbox items read.

Transitions are written at once. Counters are kept in memory and written at
most every 10 minutes (and at shutdown), merged under the lock with whatever
another worker wrote: the newest time wins and counts add up. A flood of
made-up Host or X-Forwarded-For values costs at most one write a minute and
leaves a bounded file.

The file is never restored from a backup and never overwritten when it
already exists at start (the first-boot seed included).
"""
from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .state_lock import state_write_lock

log = logging.getLogger("foodassistant.auth_state")

FILENAME = "auth-state.json"
GRACE_DAYS = 14
DAY = 86400.0
COUNTER_FLUSH_SECONDS = 600.0
SIGHTING_WRITE_SECONDS = 60.0
GATEWAY_WRITE_SECONDS = 3600.0
EPISODE_DAYS = 30
MAX_SEEN = 20
KEYLESS_FAMILIES = ("ha", "events", "reader", "display")
LEGACY_CLASSES = ("browser", "deck", "reader", "other", "same_host_proxy")

_lock = threading.RLock()
_state: dict = {
    "disk": None,         # the last document read from or written to the file
    "doc": None,          # disk plus the pending counters: what readers see
    "mtime": None,
    "memory_only": False,
    "pending": {},        # path tuple -> {"count": n, "last": iso, extras...}
    "pending_seen": {},   # host -> {"count", "last", "kind", "from"}
    "pending_other": {"count": 0, "last": None},
    "last_flush": None,
    "last_sighting_write": None,
    "last_gateway_write": None,
    "logged_unwritable": False,
}
_PROC_START = time.time()


def _prior_install_evidence() -> bool:
    """Files an earlier version leaves behind, checked once at import.

    settings.json can be missing on an install configured only through the
    environment; the database and the instance heartbeat are written by
    every earlier version, so either one marks an upgrade."""
    try:
        from ..config import settings
        base = Path(settings.data_dir)
        return any((base / name).exists() for name in ("foodassistant.db", "app-instance.json"))
    except Exception:
        return False


_PRIOR_EVIDENCE = _prior_install_evidence()


# --- Time helpers ------------------------------------------------------------


def _now(now=None) -> float:
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.timestamp()
    return float(now)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_ts(value) -> float | None:
    """An ISO 8601 string (or a number) as epoch seconds, else None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _later(a, b):
    ta, tb = parse_ts(a), parse_ts(b)
    if ta is None:
        return b
    if tb is None:
        return a
    return a if ta >= tb else b


# --- File plumbing -----------------------------------------------------------


def _path() -> Path:
    from ..config import settings
    return Path(settings.data_dir) / FILENAME


def _password_now() -> bool:
    try:
        from ..config import settings
        return bool(settings.auth_password)
    except Exception:
        return False


def _fill_defaults(doc: dict) -> dict:
    """Fill missing keys in memory (written with the next real change)."""
    doc.setdefault("v", 1)
    if doc.get("origin") not in ("fresh", "upgraded"):
        doc["origin"] = "upgraded"
    doc.setdefault("first_start", iso(_PROC_START))
    lt = doc.get("loopback_trust")
    if not isinstance(lt, dict):
        lt = {}
        doc["loopback_trust"] = lt
    if lt.get("state") not in ("legacy", "retired"):
        lt["state"] = "retired" if doc["origin"] == "fresh" else "legacy"
    lt.setdefault("since", doc["first_start"])
    lt.setdefault("by", "fresh" if doc["origin"] == "fresh" else "auto")
    lt.setdefault("retrust_until", None)
    lt.setdefault("confirm_needed", False)
    for key in ("legacy_use", "agents", "keyless_families"):
        if not isinstance(doc.get(key), dict):
            doc[key] = {}
    for key in ("keyless_ha_callers", "keyless_esp_callers", "same_host_proxy"):
        if not isinstance(doc.get(key), dict):
            doc[key] = {}
    if "password_seen" not in doc:
        doc["password_seen"] = _password_now()
    doc.setdefault("password_set_at", None)
    if not isinstance(doc.get("kiosk_gen"), int):
        doc["kiosk_gen"] = 1
    if not isinstance(doc.get("session_gen"), int):
        doc["session_gen"] = 1
    om = doc.get("open_mode")
    if not isinstance(om, dict):
        om = {}
        doc["open_mode"] = om
    if "grace_until" not in om:
        if doc["origin"] == "upgraded":
            start = parse_ts(doc.get("first_start")) or _PROC_START
            om["grace_until"] = iso(start + GRACE_DAYS * DAY)
        else:
            om["grace_until"] = None
    if not isinstance(om.get("seen"), dict):
        om["seen"] = {}
    if not isinstance(om.get("seen_other"), dict):
        om["seen_other"] = {"count": 0, "last": None}
    om.setdefault("gateway_peer_last", None)
    om.setdefault("gateway_episode", None)
    doc.setdefault("restored_at", None)
    return doc


def _upgraded_default(now: float) -> dict:
    """The lenient state: upgraded, legacy trust, a grace starting now."""
    start = iso(now)
    return _fill_defaults({
        "v": 1, "origin": "upgraded", "first_start": start,
        "loopback_trust": {"state": "legacy", "since": start, "by": "auto",
                           "retrust_until": None, "confirm_needed": False},
        "kiosk_gen": 1,
        "open_mode": {"grace_until": iso(now + GRACE_DAYS * DAY)},
    })


def _fresh_default(now: float) -> dict:
    start = iso(now)
    return _fill_defaults({
        "v": 1, "origin": "fresh", "first_start": start,
        "loopback_trust": {"state": "retired", "since": start, "by": "fresh",
                           "retrust_until": None, "confirm_needed": False},
        "kiosk_gen": 1,
        "open_mode": {"grace_until": None},
    })


def _note_unwritable(exc) -> None:
    _state["memory_only"] = True
    if not _state["logged_unwritable"]:
        _state["logged_unwritable"] = True
        log.warning("Could not write %s (%s); keeping access state in memory.", FILENAME, exc)


def _read_disk():
    """(doc, mtime) from the file, (None, None) when missing or unreadable."""
    path = _path()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None, None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None, mtime
    if not isinstance(data, dict):
        return None, mtime
    return data, mtime


def _write_disk(doc: dict) -> bool:
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".auth-state.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(doc, fh, sort_keys=True)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        _state["mtime"] = path.stat().st_mtime_ns
        return True
    except OSError as exc:
        _note_unwritable(exc)
        return False


def _node(doc: dict, path: tuple) -> dict:
    cur = doc
    for key in path:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    return cur


def _apply_counter(doc: dict, path: tuple, entry: dict) -> None:
    node = _node(doc, path)
    try:
        node["count"] = int(node.get("count") or 0) + int(entry.get("count") or 0)
    except (TypeError, ValueError):
        node["count"] = int(entry.get("count") or 0)
    node["last"] = _later(node.get("last"), entry.get("last"))
    for k, v in entry.items():
        if k not in ("count", "last"):
            node[k] = v


def _add_seen(om: dict, host: str, entry: dict) -> None:
    """Merge one sighting into open_mode.seen, keeping at most MAX_SEEN keys."""
    seen = om.setdefault("seen", {})
    other = om.setdefault("seen_other", {"count": 0, "last": None})
    cur = seen.get(host)
    if isinstance(cur, dict):
        cur["count"] = int(cur.get("count") or 0) + int(entry.get("count") or 0)
        cur["last"] = _later(cur.get("last"), entry.get("last"))
        cur.setdefault("kind", entry.get("kind"))
        cur.setdefault("from", entry.get("from"))
        return
    seen[host] = {"count": int(entry.get("count") or 0), "last": entry.get("last"),
                  "kind": entry.get("kind"), "from": entry.get("from")}
    while len(seen) > MAX_SEEN:
        oldest = min(seen, key=lambda h: parse_ts(seen[h].get("last")) or 0.0)
        gone = seen.pop(oldest)
        other["count"] = int(other.get("count") or 0) + int(gone.get("count") or 0)
        other["last"] = _later(other.get("last"), gone.get("last"))


def _apply_pending(doc: dict) -> None:
    for path, entry in _state["pending"].items():
        _apply_counter(doc, path, entry)
    om = _node(doc, ("open_mode",))
    for host, entry in _state["pending_seen"].items():
        _add_seen(om, host, entry)
    po = _state["pending_other"]
    if po.get("count"):
        other = om.setdefault("seen_other", {"count": 0, "last": None})
        other["count"] = int(other.get("count") or 0) + int(po["count"])
        other["last"] = _later(other.get("last"), po.get("last"))


def _rebuild_view() -> None:
    doc = copy.deepcopy(_state["disk"]) if _state["disk"] is not None else None
    if doc is None:
        doc = _upgraded_default(_PROC_START)
        _state["disk"] = copy.deepcopy(doc)
    _fill_defaults(doc)
    _apply_pending(doc)
    _state["doc"] = doc


def _refresh() -> dict:
    """The current view, re-read from the file when another worker changed it."""
    with _lock:
        if not _state["memory_only"]:
            try:
                mtime = _path().stat().st_mtime_ns
            except OSError:
                mtime = None
            if mtime is not None and mtime != _state["mtime"]:
                data, mt = _read_disk()
                if data is not None:
                    _state["disk"] = _fill_defaults(data)
                    _state["mtime"] = mt
                    _rebuild_view()
        if _state["doc"] is None:
            _rebuild_view()
        return _state["doc"]


def _clear_pending() -> None:
    _state["pending"] = {}
    _state["pending_seen"] = {}
    _state["pending_other"] = {"count": 0, "last": None}


def _commit(mutate=None, now: float | None = None) -> None:
    """Apply ``mutate`` to the latest document plus every pending counter and write it."""
    t = _now(now)
    with _lock:
        if _state["memory_only"]:
            base = copy.deepcopy(_state["disk"]) if _state["disk"] is not None else _upgraded_default(_PROC_START)
            _fill_defaults(base)
            if mutate:
                mutate(base)
            _apply_pending(base)
            _clear_pending()
            _state["disk"] = base
            _state["last_flush"] = t
            _rebuild_view()
            return
        with state_write_lock(_path()):
            data, _mt = _read_disk()
            if data is None:
                data = copy.deepcopy(_state["disk"]) if _state["disk"] is not None else _upgraded_default(_PROC_START)
            _fill_defaults(data)
            if mutate:
                mutate(data)
            _apply_pending(data)
            # The pending counters now live in ``data`` whether or not the
            # write worked (a failed write switches to memory only).
            _write_disk(data)
            _clear_pending()
            _state["last_flush"] = t
            _state["disk"] = data
        _rebuild_view()


def _bump(path: tuple, now: float, extras: dict | None = None, *, first_writes=True) -> None:
    """Count one event under ``path``; the first one ever is written at once."""
    t = _now(now)
    with _lock:
        doc = _refresh()
        existed = isinstance(_node_get(doc, path), dict) and bool(_node_get(doc, path).get("last"))
        entry = _state["pending"].setdefault(path, {"count": 0, "last": None})
        entry["count"] = int(entry.get("count") or 0) + 1
        entry["last"] = iso(t)
        for k, v in (extras or {}).items():
            entry[k] = v
        _apply_counter(doc, path, {"count": 1, "last": iso(t), **(extras or {})})
        if first_writes and not existed:
            _commit(now=t)
            return
        _maybe_flush(t)


def _node_get(doc: dict, path: tuple):
    cur = doc
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _maybe_flush(t: float) -> None:
    last = _state["last_flush"]
    if last is None:
        _state["last_flush"] = t
        return
    if t - last >= COUNTER_FLUSH_SECONDS:
        _commit(now=t)


def _has_pending() -> bool:
    return bool(_state["pending"] or _state["pending_seen"] or _state["pending_other"].get("count"))


# --- Public API --------------------------------------------------------------


def load() -> dict:
    """A copy of the current state."""
    return copy.deepcopy(_refresh())


def ensure_initialized(now=None) -> dict:
    """Create the file on the first start; never overwrite an existing one."""
    t = _now(now)
    from .. import config as _config
    with _lock:
        if _state["memory_only"]:
            return load()
        data, mt = _read_disk()
        if data is not None:
            _state["disk"] = _fill_defaults(data)
            _state["mtime"] = mt
            _rebuild_view()
            return load()
        try:
            exists = _path().exists()
        except OSError:
            exists = False
        if exists:
            # Unreadable or not JSON: leave it for a person to look at.
            log.warning("%s could not be read; using lenient defaults in memory.", FILENAME)
            _state["memory_only"] = True
            _state["disk"] = _upgraded_default(_PROC_START)
            _rebuild_view()
            return load()
        upgraded = bool(getattr(_config, "SETTINGS_EXISTED_AT_START", True)) or _PRIOR_EVIDENCE
        doc = _upgraded_default(t) if upgraded else _fresh_default(t)
        path = _path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".auth-state.", dir=str(path.parent))
            with os.fdopen(fd, "w") as fh:
                json.dump(doc, fh, sort_keys=True)
        except OSError as exc:
            _note_unwritable(exc)
            _state["disk"] = _upgraded_default(_PROC_START)
            _rebuild_view()
            return load()
        try:
            os.link(tmp, path)   # fails when the file appeared meanwhile
            _state["mtime"] = path.stat().st_mtime_ns
            _state["disk"] = doc
        except FileExistsError:
            data, mt = _read_disk()
            _state["disk"] = _fill_defaults(data) if data is not None else doc
            _state["mtime"] = mt
        except OSError as exc:
            _note_unwritable(exc)
            _state["disk"] = _upgraded_default(_PROC_START)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        _rebuild_view()
        return load()


def origin() -> str:
    return _refresh().get("origin", "upgraded")


def first_start() -> str | None:
    return _refresh().get("first_start")


def grace_until() -> str | None:
    return (_refresh().get("open_mode") or {}).get("grace_until")


def grace_active(now=None) -> bool:
    until = parse_ts(grace_until())
    return until is not None and _now(now) < until


def legacy_trust_active(now=None) -> bool:
    lt = _refresh().get("loopback_trust") or {}
    if lt.get("state") == "legacy":
        return True
    until = parse_ts(lt.get("retrust_until"))
    return until is not None and _now(now) < until


def retrust_active(now=None) -> bool:
    """Whether the owner chose, in Settings, to let this device's programs in
    without a key for a few days (as opposed to the trust an upgrade carried
    over)."""
    until = parse_ts((_refresh().get("loopback_trust") or {}).get("retrust_until"))
    return until is not None and _now(now) < until


def loopback_trust() -> dict:
    return dict(_refresh().get("loopback_trust") or {})


def retire(by: str = "auto", now=None) -> None:
    t = _now(now)

    def _m(doc):
        lt = doc.setdefault("loopback_trust", {})
        lt.update({"state": "retired", "since": iso(t), "by": by,
                   "retrust_until": None, "confirm_needed": False})
    _commit(_m, now=t)


def retrust(days: int = 7, now=None) -> None:
    """Let programs on this device in without a key for ``days`` days."""
    t = _now(now)

    def _m(doc):
        doc.setdefault("loopback_trust", {})["retrust_until"] = iso(t + days * DAY)
    _commit(_m, now=t)


def set_confirm_needed(flag: bool = True, now=None) -> None:
    if bool((_refresh().get("loopback_trust") or {}).get("confirm_needed")) == bool(flag):
        return

    def _m(doc):
        doc.setdefault("loopback_trust", {})["confirm_needed"] = bool(flag)
    _commit(_m, now=now)


def kiosk_gen() -> int:
    try:
        return int(_refresh().get("kiosk_gen") or 1)
    except (TypeError, ValueError):
        return 1


def session_gen() -> int:
    """The generation every signed-in browser session must carry. Bumping it
    signs out every session that was opened before."""
    try:
        return int(_refresh().get("session_gen") or 1)
    except (TypeError, ValueError):
        return 1


def bump_session_gen(now=None) -> int:
    """Sign out every browser session: after a password or two-factor change,
    or when the owner asks to sign out everywhere."""
    def _m(doc):
        try:
            doc["session_gen"] = int(doc.get("session_gen") or 1) + 1
        except (TypeError, ValueError):
            doc["session_gen"] = 2
    _commit(_m, now=now)
    return session_gen()


def session_is_current(session) -> bool:
    """Whether a browser session was opened in the current generation. A
    session from before generations existed counts as the first one, so an
    update signs nobody out; the first bump does."""
    try:
        return int(session.get("session_gen", 1)) == session_gen()
    except (TypeError, ValueError):
        return False


def stamp_session(session) -> None:
    """Mark a session as opened now, so it survives until the next bump."""
    session["session_gen"] = session_gen()


def sign_out_others(session) -> int:
    """Bump the generation and keep only ``session`` signed in."""
    gen = bump_session_gen()
    session["session_gen"] = gen
    return gen


def bump_kiosk_gen(now=None) -> int:
    def _m(doc):
        try:
            doc["kiosk_gen"] = int(doc.get("kiosk_gen") or 1) + 1
        except (TypeError, ValueError):
            doc["kiosk_gen"] = 2
    _commit(_m, now=now)
    return kiosk_gen()


def note_restore(now=None) -> None:
    """A restore brought settings.json: count as upgraded with a new grace."""
    t = _now(now)

    def _m(doc):
        doc["origin"] = "upgraded"
        doc["restored_at"] = iso(t)
        om = doc.setdefault("open_mode", {})
        om["grace_until"] = iso(t + GRACE_DAYS * DAY)
        om["seen"] = {}
        om["seen_other"] = {"count": 0, "last": None}
    with _lock:
        _state["pending_seen"] = {}
        _state["pending_other"] = {"count": 0, "last": None}
        _commit(_m, now=t)


# Left in data_dir by the device's full-stack restore helper
# (foodassistant-restore, and scripts/restore.sh) when the snapshot brought a
# different settings.json. The app cannot see that restore happen, so the
# next start does what the in-app restore does at once: note_restore. A
# device-local file, never archived and never restored.
RESTORE_MARKER = "restore-note-pending"


def consume_restore_marker(now=None) -> bool:
    """Run note_restore once for a restore done outside the app.

    The marker is removed first, so with several workers starting together
    only the one whose unlink succeeds records the restore, and a marker that
    cannot be removed never re-opens the grace on every start."""
    from ..config import settings
    path = Path(settings.data_dir) / RESTORE_MARKER
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("Could not clear %s, so the restore is not recorded: %s",
                    RESTORE_MARKER, exc)
        return False
    note_restore(now)
    log.info("Recorded a restore made on the device: access counts as upgraded "
             "with a fresh %d-day grace.", GRACE_DAYS)
    return True


def flush(force: bool = False, now=None) -> None:
    """Write pending counters (at most every 10 minutes unless forced)."""
    t = _now(now)
    with _lock:
        if not _has_pending():
            return
        last = _state["last_flush"]
        if force or last is None or t - last >= COUNTER_FLUSH_SECONDS:
            _commit(now=t)


def reset(state: dict | None = None, persist: bool = True) -> None:
    """Replace the state (tests). persist=False never touches the disk."""
    with _lock:
        _clear_pending()
        _state["mtime"] = None
        _state["last_flush"] = None
        _state["last_sighting_write"] = None
        _state["last_gateway_write"] = None
        _state["logged_unwritable"] = False
        _state["memory_only"] = not persist
        if state is None:
            _state["disk"] = None
            _state["doc"] = None
            if persist:
                try:
                    _path().unlink(missing_ok=True)
                except OSError:
                    pass
            return
        _state["disk"] = _fill_defaults(copy.deepcopy(state))
        if persist:
            _write_disk(_state["disk"])
        _rebuild_view()


def test_state(**overrides) -> dict:
    """An upgraded install with legacy trust and a grace starting now."""
    now = overrides.pop("now", None)
    doc = _upgraded_default(_now(now))
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(doc.get(key), dict):
            doc[key].update(value)
        else:
            doc[key] = value
    return doc


# Agents and legacy use ---------------------------------------------------------


def relaunches_today(now=None) -> list[float]:
    t = _now(now)
    day = datetime.fromtimestamp(t).date()
    raw = ((_refresh().get("agents") or {}).get("screen") or {}).get("relaunches") or []
    out = []
    for value in raw:
        ts = parse_ts(value)
        if ts is not None and datetime.fromtimestamp(ts).date() == day:
            out.append(ts)
    return sorted(out)


def record_relaunch(now=None) -> None:
    t = _now(now)
    keep = [iso(x) for x in relaunches_today(t)] + [iso(t)]

    def _m(doc):
        screen = _node(doc, ("agents", "screen"))
        screen["relaunches"] = keep[-10:]
    _commit(_m, now=t)


def record_ticket_redeemed(now=None) -> None:
    t = _now(now)

    def _m(doc):
        _node(doc, ("agents", "screen"))["last_ticket"] = iso(t)
    _commit(_m, now=t)


def record_agent(agent: str, caps=(), origin: str = "", version: str = "", now=None) -> None:
    """Count one use of an agent key (the deck or the reader)."""
    if agent not in ("streamdeck", "gadgets"):
        return
    t = _now(now)
    extras: dict = {"version": str(version or "")[:64]}
    if agent == "streamdeck":
        caps = sorted(str(c) for c in (caps or ()))[:16]
        extras["caps"] = caps
        extras["origin"] = str(origin or "")[:200]
        if "navorigin1" in caps:
            extras["nav_last"] = iso(t)
    _bump(("agents", agent), t, extras)


def record_legacy_use(cls: str, ua: str = "", path: str = "", now=None) -> None:
    if cls not in LEGACY_CLASSES:
        cls = "other"
    _bump(("legacy_use", cls), _now(now), {"ua": str(ua or "")[:120], "path": str(path or "")[:200]})


def record_keyless_family(family: str, ua: str = "", peer: str = "", now=None) -> None:
    if family not in KEYLESS_FAMILIES:
        return
    _bump(("keyless_families", family), _now(now),
          {"ua": str(ua or "")[:120], "peer": str(peer or "")[:64]})


def record_keyless_ha(peer: str = "", now=None) -> None:
    _bump(("keyless_ha_callers",), _now(now), {"peer": str(peer or "")[:64]})


def record_keyless_esp(peer: str = "", now=None) -> None:
    _bump(("keyless_esp_callers",), _now(now), {"peer": str(peer or "")[:64]})


def _within(value, t: float, days: float, since=None) -> bool:
    ts = parse_ts(value)
    if ts is None or ts < t - days * DAY:
        return False
    if since is not None:
        s = parse_ts(since)
        if s is not None and ts < s:
            return False
    return True


def keyless_device_labels(days: float = 30, since=None, now=None) -> list[str]:
    """The kinds of keyless home devices seen in the window, in a fixed order."""
    from .access import KEYLESS_LABELS
    t = _now(now)
    doc = _refresh()
    fams = doc.get("keyless_families") or {}

    def _seen(node) -> bool:
        return isinstance(node, dict) and _within(node.get("last"), t, days, since)

    out: list[str] = []
    if _seen(fams.get("ha")) or _seen(fams.get("events")) or _seen(doc.get("keyless_ha_callers")):
        out.append(KEYLESS_LABELS["ha"])
    if _seen(fams.get("display")):
        out.append(KEYLESS_LABELS["display"])
    if _seen(doc.get("keyless_esp_callers")):
        out.append(KEYLESS_LABELS["esp"])
    if _seen(fams.get("reader")):
        out.append(KEYLESS_LABELS["reader"])
    return out


def note_password_state(password_set: bool, now=None) -> bool:
    """True exactly when a first password just appeared (stored False, now True)."""
    t = _now(now)
    seen = bool(_refresh().get("password_seen"))
    if bool(password_set) == seen:
        return False
    if password_set:
        def _m(doc):
            doc["password_seen"] = True
            doc["password_set_at"] = iso(t)
        _commit(_m, now=t)
        return True

    def _clear(doc):
        doc["password_seen"] = False
    _commit(_clear, now=t)
    return False


def password_set_at() -> str | None:
    return _refresh().get("password_set_at")


def record_same_host_proxy(signal: str, now=None) -> bool:
    """Count a request that came through a same-host proxy; True for a new episode."""
    t = _now(now)
    with _lock:
        doc = _refresh()
        node = doc.get("same_host_proxy") or {}
        last = parse_ts(node.get("last"))
        if last is None or t - last > EPISODE_DAYS * DAY:
            def _m(d):
                n = d.setdefault("same_host_proxy", {})
                n["episode"] = datetime.fromtimestamp(t, timezone.utc).date().isoformat()
                n["last"] = iso(t)
                n["count"] = int(n.get("count") or 0) + 1
                n["signal"] = str(signal or "")[:16]
            _commit(_m, now=t)
            return True
        _bump(("same_host_proxy",), t, {"signal": str(signal or "")[:16]}, first_writes=False)
        return False


def same_host_proxy_recent(now=None) -> bool:
    node = _refresh().get("same_host_proxy") or {}
    return _within(node.get("last"), _now(now), EPISODE_DAYS)


# Open-mode sightings -----------------------------------------------------------


def record_open_sighting(host: str, kind: str, frm: str = "", now=None) -> bool:
    """Count a proxied, public or foreign-name request seen during the grace.

    True only for a host not seen before in this grace whose sighting was
    written right away (at most one such write a minute), so a flood of new
    names cannot turn into a flood of inbox work."""
    t = _now(now)
    host = str(host or "a proxy")[:253]
    kind = kind if kind in ("proxied", "public", "name") else "proxied"
    frm = str(frm or "")[:64]
    with _lock:
        doc = _refresh()
        om = _node(doc, ("open_mode",))
        stamp = iso(t)
        known = host in (om.get("seen") or {})
        pending = _state["pending_seen"]
        if known or host in pending:
            entry = pending.setdefault(host, {"count": 0, "last": None, "kind": kind, "from": frm})
            entry["count"] += 1
            entry["last"] = stamp
            _add_seen(om, host, {"count": 1, "last": stamp, "kind": kind, "from": frm})
            _maybe_flush(t)
            return False
        # A new host.
        pending[host] = {"count": 1, "last": stamp, "kind": kind, "from": frm}
        while len(pending) > MAX_SEEN:
            oldest = min(pending, key=lambda h: parse_ts(pending[h].get("last")) or 0.0)
            gone = pending.pop(oldest)
            po = _state["pending_other"]
            po["count"] = int(po.get("count") or 0) + int(gone.get("count") or 0)
            po["last"] = _later(po.get("last"), gone.get("last"))
        _add_seen(om, host, {"count": 1, "last": stamp, "kind": kind, "from": frm})
        last_write = _state["last_sighting_write"]
        if last_write is None or t - last_write >= SIGHTING_WRITE_SECONDS:
            _state["last_sighting_write"] = t
            _commit(now=t)
            return True
        return False


def open_sightings() -> tuple[list[dict], int]:
    """(sightings newest first, count of older sightings folded together)."""
    om = _refresh().get("open_mode") or {}
    seen = om.get("seen") or {}
    rows = [{"host": h, "kind": v.get("kind"), "count": v.get("count", 0),
             "last": v.get("last"), "from": v.get("from")}
            for h, v in seen.items() if isinstance(v, dict)]
    rows.sort(key=lambda r: parse_ts(r.get("last")) or 0.0, reverse=True)
    other = om.get("seen_other") or {}
    try:
        other_count = int(other.get("count") or 0)
    except (TypeError, ValueError):
        other_count = 0
    return rows, other_count


def record_gateway_peer(now=None) -> bool:
    """Note a Docker-gateway peer on an open install; True for a new episode."""
    t = _now(now)
    with _lock:
        om = _refresh().get("open_mode") or {}
        last = parse_ts(om.get("gateway_peer_last"))
        if last is None or t - last > EPISODE_DAYS * DAY:
            def _m(doc):
                o = doc.setdefault("open_mode", {})
                o["gateway_peer_last"] = iso(t)
                o["gateway_episode"] = datetime.fromtimestamp(t, timezone.utc).date().isoformat()
            _state["last_gateway_write"] = t
            _commit(_m, now=t)
            return True
        lw = _state["last_gateway_write"]
        if lw is None or t - lw >= GATEWAY_WRITE_SECONDS:
            _state["last_gateway_write"] = t

            def _m2(doc):
                doc.setdefault("open_mode", {})["gateway_peer_last"] = iso(t)
            _commit(_m2, now=t)
        else:
            _node(_state["doc"], ("open_mode",))["gateway_peer_last"] = iso(t)
        return False


def gateway_episode() -> str | None:
    return (_refresh().get("open_mode") or {}).get("gateway_episode")


def gateway_peer_recent(now=None) -> bool:
    om = _refresh().get("open_mode") or {}
    return _within(om.get("gateway_peer_last"), _now(now), EPISODE_DAYS)
