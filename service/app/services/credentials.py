"""API keys: data_dir/credentials.json.

Two kinds of key reach this app:

- pr1 keys, minted here from 0.21 on: 'pr1.' + id + '.' + 43 characters of
  random base64url. Only a sha256 of the whole key is stored. Each one has a
  scope (what it may do), and while legacy_mirror is on a copy is also kept
  in extra_api_keys so a rollback to an older version keeps accepting it.
- legacy keys: settings.api_key, the extra_api_keys entries that are not
  mirrors, and an API_KEY from the environment. They keep working with the
  legacy scope (everything except Settings). A record (id 'lg_' + 16 hex of
  the key's sha256) is created the first time one is used, so the Security
  pane can show who still uses it and narrow or retire it.

The file follows the state-file pattern (lock, a durable write through
durable.py with a last good copy beside it, 0600, mtime-cached reads, quiet
in-memory fallback). Use metadata is written
at most every 60 seconds; a first use, a new holder, a new puller and every
change of status or scope are written at once. A damaged file is put back
from its last good copy with a notice in the inbox; with no good copy it is
set aside, starts empty and raises one inbox item, and pr1 keys fail closed.
"""
from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .state_lock import state_write_lock

log = logging.getLogger("foodassistant.credentials")

FILENAME = "credentials.json"
MINTABLE_SCOPES = ("admin", "satellite", "ha", "ha_rest", "display")
NARROW_SCOPES = ("satellite", "ha", "ha_rest", "display")
LEGACY_SCOPES = ("legacy",) + NARROW_SCOPES
ADMIN_TTL_DAYS = 90
USE_FLUSH_SECONDS = 60.0
DAY = 86400.0
MAX_FAMILIES = 16
MAX_REFUSALS = 20
MAX_CAPS = 16
MAX_PULLERS = 16
MAX_HOLDERS = 16
SHARED_KEY_SETTLE_SECONDS = 86400.0
_PR1_RE = re.compile(r"^pr1\.(c_[0-9a-f]{16})\.([A-Za-z0-9_-]{43})$")


class CredentialError(Exception):
    """A change the rules refuse; str(exc) is the message to show."""


_lock = threading.RLock()
_state: dict = {
    "records": None,       # id -> record (disk plus pending use)
    "disk": None,
    "mtime": None,
    "memory_only": False,
    "pending": {},         # id -> pending use updates
    "last_flush": None,
}


def _now(now=None) -> float:
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.timestamp()
    return float(now)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ts(value) -> float | None:
    from .auth_state import parse_ts
    return parse_ts(value)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def legacy_id(key: str) -> str:
    return "lg_" + _sha(key)[:16]


def _path() -> Path:
    from ..config import settings
    return Path(settings.data_dir) / FILENAME


def _settings():
    from ..config import settings
    return settings


# --- File plumbing -----------------------------------------------------------


def _valid_doc(data) -> bool:
    return isinstance(data, dict) and isinstance(data.get("records", {}), dict)


def _note_lost() -> None:
    log.error("%s could not be read and had no good copy; it was set aside and "
              "keys minted here need to be created again.", FILENAME)
    try:
        from .access import raise_item
        raise_item("credentials_unreadable",
                   "Saved keys could not be read",
                   "The file that holds this kitchen's keys could not be read, so "
                   "it was set aside and keys created in Settings, Security stop "
                   "working. Older keys still work. Create new keys for the "
                   "devices that stopped connecting.",
                   "credentials_unreadable", level="warning")
    except Exception:
        pass


def _read_disk():
    """(doc, mtime) from the file. A damaged file is put back from its last
    good copy (durable.py); with no good copy it is set aside and the store
    starts empty, so pr1 keys fail closed."""
    from .. import durable
    loaded = durable.read_json(_path(), validate=_valid_doc,
                               what="The file of saved keys",
                               notify_damaged=False)
    if loaded.state == durable.MISSING:
        return None, None
    if loaded.state == durable.UNREADABLE:
        return None, loaded.mtime_ns
    if loaded.state == durable.DAMAGED:
        _note_lost()
        return {"v": 1, "records": {}}, None
    return loaded.doc, loaded.mtime_ns


def _write_disk(doc: dict) -> bool:
    from .. import durable
    try:
        _state["mtime"] = durable.write_json(_path(), doc, sort_keys=True,
                                             validate=_valid_doc)
        return True
    except OSError as exc:
        if not _state["memory_only"]:
            log.warning("Could not write %s (%s); keeping keys in memory.", FILENAME, exc)
        _state["memory_only"] = True
        return False


def _merge_list(items: list, new: dict, key: str, cap: int, order: str) -> list:
    out = [i for i in items if isinstance(i, dict) and i.get(key) != new.get(key)]
    cur = next((i for i in items if isinstance(i, dict) and i.get(key) == new.get(key)), None)
    merged = dict(cur or {})
    merged.update(new)
    out.append(merged)
    while len(out) > cap:
        oldest = min(out, key=lambda i: _ts(i.get(order)) or 0.0)
        out.remove(oldest)
    return out


def _apply_pending_one(rec: dict, pend: dict) -> None:
    use = pend.get("use")
    if use:
        if (_ts(use.get("last_used")) or 0) >= (_ts(rec.get("last_used")) or 0):
            rec["last_used"] = use.get("last_used")
            rec["last_ip"] = use.get("last_ip", "")
            rec["last_client"] = use.get("last_client", "")
    for fam in (pend.get("families") or {}).values():
        fams = rec.setdefault("families", [])
        cur = next((f for f in fams if f.get("family") == fam["family"]
                    and f.get("method") == fam["method"]), None)
        if cur is None:
            fams.append(copy.deepcopy(fam))
            while len(fams) > MAX_FAMILIES:
                fams.remove(min(fams, key=lambda f: _ts(f.get("last")) or 0.0))
        else:
            paths = [p for p in cur.get("paths", []) if p not in fam.get("paths", [])]
            cur["paths"] = (paths + list(fam.get("paths", [])))[-4:]
            if (_ts(fam.get("last")) or 0) >= (_ts(cur.get("last")) or 0):
                cur["last"] = fam.get("last")
    for holder in (pend.get("holders") or {}).values():
        rec["holders"] = _merge_list(rec.get("holders") or [], holder, "key", MAX_HOLDERS, "last")
    for puller in (pend.get("pullers") or {}).values():
        rec["pullers"] = _merge_list(rec.get("pullers") or [], puller, "key", MAX_PULLERS, "pulled")
    for ref in pend.get("refusals") or []:
        rec.setdefault("refusals", []).append(ref)
        rec["refusals"] = rec["refusals"][-MAX_REFUSALS:]
    caps = pend.get("caps")
    if caps:
        merged = list(rec.get("caps_seen") or [])
        for c in caps:
            if c not in merged:
                merged.append(c)
        rec["caps_seen"] = merged[:MAX_CAPS]
    if pend.get("first_pull") and not rec.get("first_pull"):
        rec["first_pull"] = pend["first_pull"]
    if pend.get("reactivate") and rec.get("status") == "retiring":
        rec["status"] = "active"


def _rebuild() -> None:
    disk = _state["disk"] or {"v": 1, "records": {}}
    recs = copy.deepcopy(disk.get("records") or {})
    for cid, pend in _state["pending"].items():
        if cid in recs:
            _apply_pending_one(recs[cid], pend)
        elif pend.get("record"):
            rec = copy.deepcopy(pend["record"])
            _apply_pending_one(rec, pend)
            recs[cid] = rec
    _state["records"] = recs


def _records() -> dict:
    with _lock:
        if not _state["memory_only"]:
            try:
                mtime = _path().stat().st_mtime_ns
            except OSError:
                mtime = None
            if mtime is not None and mtime != _state["mtime"]:
                data, mt = _read_disk()
                if data is not None:
                    _state["disk"] = data
                    _state["mtime"] = mt
                    _rebuild()
        if _state["records"] is None:
            if _state["disk"] is None and not _state["memory_only"]:
                data, mt = _read_disk()
                _state["disk"] = data if data is not None else {"v": 1, "records": {}}
                _state["mtime"] = mt
            _rebuild()
        return _state["records"]


def _commit(mutate=None, now=None) -> None:
    t = _now(now)
    with _lock:
        memory = _state["memory_only"]
        cm = contextlib.nullcontext() if memory else state_write_lock(_path())
        with cm:
            doc = None
            if not memory:
                data, _mt = _read_disk()
                doc = data
            if doc is None:
                doc = copy.deepcopy(_state["disk"] or {"v": 1, "records": {}})
            recs = doc.setdefault("records", {})
            for cid, pend in _state["pending"].items():
                if cid in recs:
                    _apply_pending_one(recs[cid], pend)
                elif pend.get("record"):
                    rec = copy.deepcopy(pend["record"])
                    _apply_pending_one(rec, pend)
                    recs[cid] = rec
            if mutate:
                mutate(recs)
            if not memory:
                _write_disk(doc)
            _state["pending"] = {}
            _state["disk"] = doc
            _state["last_flush"] = t
        _rebuild()


def flush(force: bool = True, now=None) -> None:
    """Write pending use metadata."""
    t = _now(now)
    with _lock:
        if not _state["pending"]:
            return
        last = _state["last_flush"]
        if force or last is None or t - last >= USE_FLUSH_SECONDS:
            _commit(now=t)


def _maybe_flush(t: float) -> None:
    last = _state["last_flush"]
    if last is None or t - last >= USE_FLUSH_SECONDS:
        _commit(now=t)


def reset(records: dict | None = None, persist: bool = True) -> None:
    """Replace every record (tests). persist=False never touches the disk."""
    with _lock:
        _state["pending"] = {}
        _state["mtime"] = None
        _state["last_flush"] = None
        _state["memory_only"] = not persist
        _state["disk"] = {"v": 1, "records": copy.deepcopy(records or {})}
        _state["records"] = None
        if persist:
            if records is None:
                from .. import durable
                for p in (_path(), durable.last_good_path(_path())):
                    try:
                        p.unlink(missing_ok=True)
                    except OSError:
                        pass
            else:
                _write_disk(_state["disk"])
        _rebuild()


# --- Lookup ------------------------------------------------------------------


def _expired(rec: dict, t: float) -> bool:
    exp = _ts(rec.get("expires"))
    return exp is not None and t >= exp


def _legacy_sources() -> list[tuple[str, str, int]]:
    """(key, source, extra index or -1) for every legacy key in settings."""
    s = _settings()
    out = []
    if s.api_key:
        src = "env" if _env_api_key() == s.api_key else "api_key"
        out.append((s.api_key, src, -1))
    extras = s.extra_api_keys if isinstance(s.extra_api_keys, list) else []
    for i, k in enumerate(extras):
        if isinstance(k, str) and k and not k.startswith("pr1."):
            out.append((k, "extra", i))
    return out


def _env_api_key() -> str:
    """API_KEY from the container environment, '' when unset.

    The primary key came from the environment when it equals this value; such
    a key can only be turned off there."""
    for name, value in os.environ.items():
        if name.upper() == "API_KEY" and value:
            return value
    return ""


def _new_legacy_record(key: str, source: str, t: float) -> dict:
    return {
        "id": legacy_id(key), "name": "", "scope": "legacy", "kind": "legacy",
        "secret_sha256": "", "device_id": None, "created": _iso(t), "created_by": "",
        "last_used": None, "last_ip": "", "last_client": "", "families": [],
        "refusals": [], "status": "active", "expires": None, "caps_seen": [],
        "relay_capable_at": None, "pullers": [], "holders": [], "source": source,
    }


def _ensure_legacy(key: str, source: str, t: float) -> dict:
    """The legacy record for a key, created in memory on first sight."""
    cid = legacy_id(key)
    recs = _records()
    rec = recs.get(cid)
    if rec is None:
        rec = _new_legacy_record(key, source, t)
        _state["pending"].setdefault(cid, {})["record"] = copy.deepcopy(rec)
        recs[cid] = rec
    elif rec.get("source") != source and rec.get("kind") == "legacy":
        rec["source"] = source
    return rec


def _key_bytes(value: str, encoding: str) -> bytes:
    try:
        return value.encode(encoding)
    except UnicodeEncodeError:
        return value.encode("utf-8", "surrogatepass")


def lookup(secret: str, now=None) -> dict | None:
    """The record a presented key belongs to, or None (constant-time compares)."""
    if not secret or not isinstance(secret, str):
        return None
    t = _now(now)
    m = _PR1_RE.match(secret)
    if m:
        with _lock:
            rec = _records().get(m.group(1))
            digest = _sha(secret)
            if rec is None or rec.get("kind") == "legacy":
                secrets.compare_digest(digest, "0" * 64)
                return None
            if not secrets.compare_digest(digest, str(rec.get("secret_sha256") or "")):
                return None
            if rec.get("status") == "revoked" or _expired(rec, t):
                return None
            return copy.deepcopy(rec)
    # Compare bytes: compare_digest refuses str with non-ASCII characters, and
    # a header can carry any byte (Starlette decodes headers as latin-1).
    sent = _key_bytes(secret, "latin-1")
    match = None
    for key, source, _idx in _legacy_sources():
        if secrets.compare_digest(sent, _key_bytes(key, "utf-8")) and match is None:
            match = (key, source)
    if match is None:
        return None
    with _lock:
        rec = _ensure_legacy(match[0], match[1], t)
        if rec.get("status") == "revoked":
            return None
        return copy.deepcopy(rec)


def get(cid: str) -> dict | None:
    rec = _records().get(cid or "")
    return copy.deepcopy(rec) if rec else None


def ensure_legacy_records(now=None) -> None:
    """Create the records for every legacy key in settings (the Security pane)."""
    t = _now(now)
    with _lock:
        for key, source, _idx in _legacy_sources():
            _ensure_legacy(key, source, t)


def all_records() -> list[dict]:
    return [copy.deepcopy(r) for r in _records().values()]


def any_credential_exists() -> bool:
    """True when any key could authenticate here (legacy or minted)."""
    if _legacy_sources():
        return True
    t = time.time()
    return any(r.get("kind") != "legacy" and r.get("status") != "revoked"
               and not _expired(r, t) for r in _records().values())


# --- Minting, revoking, scopes -----------------------------------------------


def _new_secret() -> tuple[str, str]:
    cid = "c_" + secrets.token_hex(8)
    tail = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    return cid, f"pr1.{cid}.{tail}"


def _padded_names(keys: list, names: list) -> list:
    names = list(names)
    names += [""] * (len(keys) - len(names))
    return names[:len(keys)]


def mint(scope: str, name: str, kind: str, device_id=None, created_by: str = "",
         expires=None, now=None) -> tuple[dict, str]:
    """Create a key. Returns (record, secret); the secret is never stored."""
    if scope not in MINTABLE_SCOPES:
        raise CredentialError("That kind of key cannot be created.")
    t = _now(now)
    cid, secret = _new_secret()
    if expires is None and scope == "admin":
        expires = _iso(t + ADMIN_TTL_DAYS * DAY)
    name = str(name or "").strip()[:60] or "Key"
    rec = {
        "id": cid, "name": name, "scope": scope, "minted_scope": scope,
        "kind": kind if kind in ("paired", "manual", "exchanged", "device") else "manual",
        "secret_sha256": _sha(secret), "device_id": device_id, "created": _iso(t),
        "created_by": str(created_by or "")[:60], "last_used": None, "last_ip": "",
        "last_client": "", "families": [], "refusals": [], "status": "active",
        "expires": expires, "caps_seen": [], "relay_capable_at": None, "pullers": [],
        "mirrored": False,
    }
    s = _settings()
    if s.legacy_mirror:
        keys = [k for k in (s.extra_api_keys if isinstance(s.extra_api_keys, list) else [])]
        names = _padded_names(keys, s.extra_api_key_names if isinstance(s.extra_api_key_names, list) else [])
        s.save({"extra_api_keys": keys + [secret],
                "extra_api_key_names": names + ["(mirror) " + name]})
        rec["mirrored"] = True

    def _m(recs):
        recs[cid] = rec
    _commit(_m, now=t)
    return copy.deepcopy(rec), secret


def _remove_extra_where(match) -> bool:
    """Drop every extra key (and its name) for which match(key) is true."""
    s = _settings()
    keys = list(s.extra_api_keys if isinstance(s.extra_api_keys, list) else [])
    names = _padded_names(keys, s.extra_api_key_names if isinstance(s.extra_api_key_names, list) else [])
    keep_k, keep_n = [], []
    removed = False
    for k, n in zip(keys, names):
        if isinstance(k, str) and match(k):
            removed = True
            continue
        keep_k.append(k)
        keep_n.append(n)
    if removed:
        s.save({"extra_api_keys": keep_k, "extra_api_key_names": keep_n})
    return removed


def _remove_mirror(rec: dict) -> None:
    digest = str(rec.get("secret_sha256") or "")
    if digest:
        _remove_extra_where(lambda k: k.startswith("pr1.") and secrets.compare_digest(_sha(k), digest))


def revoke(cid: str, now=None) -> dict:
    """Turn a key off for good. Raises KeyError (unknown) or CredentialError."""
    from .access import ENV_KEY_MSG
    t = _now(now)
    with _lock:
        rec = _records().get(cid or "")
        if rec is None:
            if str(cid or "").startswith("lg_"):
                ensure_legacy_records(t)
                rec = _records().get(cid)
            if rec is None:
                raise KeyError(cid)
        if rec.get("kind") == "legacy":
            if rec.get("source") == "env":
                raise CredentialError(ENV_KEY_MSG)
            s = _settings()
            if rec.get("source") == "api_key" and s.api_key and legacy_id(s.api_key) == cid:
                s.save({"api_key": ""})
            else:
                _remove_extra_where(lambda k: not k.startswith("pr1.") and legacy_id(k) == cid)
        else:
            _remove_mirror(rec)

    def _m(recs):
        r = recs.get(cid)
        if r is None and rec is not None:
            r = copy.deepcopy(rec)
            recs[cid] = r
        r["status"] = "revoked"
        r["revoked"] = _iso(t)
        r["mirrored"] = False
    _commit(_m, now=t)
    return get(cid) or {}


def set_scope(cid: str, scope: str, now=None) -> str:
    """Change a key's scope; returns the previous one. Never raises a key to admin."""
    with _lock:
        rec = _records().get(cid or "")
        if rec is None:
            if str(cid or "").startswith("lg_"):
                ensure_legacy_records(now)
                rec = _records().get(cid)
            if rec is None:
                raise KeyError(cid)
        previous = rec.get("scope")
        if rec.get("status") == "revoked":
            raise CredentialError("That key was revoked.")
        if rec.get("kind") == "legacy":
            allowed = LEGACY_SCOPES
        else:
            allowed = NARROW_SCOPES + (("admin",) if rec.get("minted_scope") == "admin" else ())
        if scope not in allowed:
            raise CredentialError("A key cannot be given more access than it was created with.")

    def _m(recs):
        r = recs.get(cid)
        if r is None:
            r = copy.deepcopy(rec)
            recs[cid] = r
        r["scope"] = scope
    _commit(_m, now=now)
    return previous


def remove_all_mirrors() -> bool:
    removed = _remove_extra_where(lambda k: k.startswith("pr1."))

    def _m(recs):
        for r in recs.values():
            if r.get("mirrored"):
                r["mirrored"] = False
    _commit(_m)
    return removed


# --- Use, refusals, caps -----------------------------------------------------


def _holder_key(device_id: str, client: str, ua: str, peer: str) -> str:
    if device_id:
        return str(device_id)[:64]
    fam = (client or "").split("/")[0] if client else ""
    if not fam:
        fam = ((ua or "").split() or [""])[0].split("/")[0]
    return f"{fam[:32]}:{str(peer or '')[:64]}"


def record_use(cid: str, meta: dict | None = None, now=None) -> None:
    """Note one authenticated use. Written at most every 60 s; the first at once."""
    if not cid or cid.startswith("agent:"):
        return
    t = _now(now)
    meta = meta or {}
    with _lock:
        recs = _records()
        rec = recs.get(cid)
        if rec is None:
            return
        first = not rec.get("last_used")
        pend = _state["pending"].setdefault(cid, {})
        stamp = _iso(t)
        pend["use"] = {"last_used": stamp, "last_ip": str(meta.get("ip") or "")[:64],
                       "last_client": str(meta.get("client") or "")[:64]}
        write_now = first
        fam = meta.get("family")
        if fam:
            method = str(meta.get("method") or "GET").upper()
            fams = pend.setdefault("families", {})
            key = f"{fam}|{method}"
            entry = fams.setdefault(key, {"family": fam, "method": method, "paths": [], "last": stamp})
            path = str(meta.get("path") or "")[:200]
            if path and path not in entry["paths"]:
                entry["paths"] = (entry["paths"] + [path])[-4:]
            entry["last"] = stamp
        device_id = str(meta.get("device_id") or "")[:64]
        if rec.get("kind") == "legacy":
            hkey = _holder_key(device_id, meta.get("client", ""), meta.get("ua", ""), meta.get("ip", ""))
            existing = next((h for h in rec.get("holders") or [] if h.get("key") == hkey), None)
            holder = {"key": hkey, "device_id": device_id or None,
                      "client": str(meta.get("client") or "")[:64],
                      "ua": str(meta.get("ua") or "")[:120], "peer": str(meta.get("ip") or "")[:64],
                      "device_mode": str(meta.get("device_mode") or "")[:32],
                      "cub": bool(meta.get("cub")), "moved": False, "last": stamp}
            pend.setdefault("holders", {})[hkey] = holder
            if existing is None:
                write_now = True
            elif existing.get("moved"):
                write_now = True
            if rec.get("status") == "retiring":
                pend["reactivate"] = True
                write_now = True
        elif device_id:
            # A device that now signs in with its own key has moved off the
            # shared legacy keys it used before.
            for other in recs.values():
                if other.get("kind") != "legacy":
                    continue
                for h in other.get("holders") or []:
                    if h.get("device_id") == device_id and not h.get("moved"):
                        moved = dict(h)
                        moved["moved"] = True
                        _state["pending"].setdefault(other["id"], {}).setdefault(
                            "holders", {})[h["key"]] = moved
                        write_now = True
        _rebuild()
        if write_now:
            _commit(now=t)
        else:
            _maybe_flush(t)


def record_refusal(cid: str, path: str, now=None) -> None:
    if not cid or cid.startswith("agent:"):
        return
    t = _now(now)
    with _lock:
        rec = _records().get(cid)
        if rec is None:
            return
        ref = {"path": str(path or "")[:200], "at": _iso(t)}
        _state["pending"].setdefault(cid, {}).setdefault("refusals", []).append(ref)
        _rebuild()
        _maybe_flush(t)


def mark_caps(cid: str, caps, *, device_id: str = "", client: str = "", ua: str = "",
              peer: str = "", now=None) -> None:
    """Record one satellite pull and decide the relay ratchet.

    A key is ratcheted (pulls without relay1 get the cut-off payload) only once
    every device that pulled with it in 30 days has sent relay1, and never
    when it is the primary or environment key."""
    if not cid or cid.startswith("agent:"):
        return
    t = _now(now)
    caps = sorted({str(c) for c in (caps or ())})
    stamp = _iso(t)
    with _lock:
        recs = _records()
        rec = recs.get(cid)
        if rec is None:
            return
        pend = _state["pending"].setdefault(cid, {})
        key = _holder_key(device_id, client, ua, peer)
        pullers = [dict(p) for p in rec.get("pullers") or []]
        cur = next((p for p in pullers if p.get("key") == key), None)
        write_now = cur is None
        puller = dict(cur or {"key": key, "relay1_at": None})
        puller["pulled"] = stamp
        # Whether this pull asked for the relay: a ratcheted key whose device
        # went back to a version before 0.21 shows in Settings, Security.
        puller["relay"] = "relay1" in caps
        if "relay1" in caps and not puller.get("relay1_at"):
            puller["relay1_at"] = stamp
            write_now = True
        pend.setdefault("pullers", {})[key] = puller
        pend["caps"] = list(dict.fromkeys(list(pend.get("caps") or []) + caps))
        if not rec.get("first_pull"):
            pend.setdefault("first_pull", stamp)
            write_now = True
        _rebuild()
        rec = _state["records"].get(cid) or rec
        # An older shared key also needs a day of pulls on record first (from
        # its first recorded pull), so a second satellite that shares it and
        # pulls every few minutes is seen before a relay1 pull can ratchet it.
        settled = (rec.get("kind") != "legacy"
                   or (_ts(rec.get("first_pull")) or t) <= t - SHARED_KEY_SETTLE_SECONDS)
        if (not rec.get("relay_capable_at") and "relay1" in caps and settled
                and rec.get("source") not in ("api_key", "env")):
            recent = [p for p in rec.get("pullers") or []
                      if (_ts(p.get("pulled")) or 0) >= t - 30 * DAY]
            if recent and all(p.get("relay1_at") for p in recent):
                def _m(recs_):
                    r = recs_.get(cid)
                    if r is not None and not r.get("relay_capable_at"):
                        r["relay_capable_at"] = stamp
                _commit(_m, now=t)
                return
        if write_now:
            _commit(now=t)
        else:
            _maybe_flush(t)


def relay_ratcheted(cid: str) -> bool:
    rec = _records().get(cid or "")
    return bool(rec and rec.get("relay_capable_at"))


def service_keys_withheld(rec: dict | None, now=None) -> bool:
    """Whether the ratchet holds service keys back from a device right now:
    the key is ratcheted, and a device pulled with it in the last 30 days
    without relay1 (a Bandit put back on a version before 0.21)."""
    if not rec or not rec.get("relay_capable_at"):
        return False
    t = _now(now)
    return any(isinstance(p, dict) and p.get("relay") is False
               and (_ts(p.get("pulled")) or 0) >= t - 30 * DAY
               for p in rec.get("pullers") or [])


def clear_relay_ratchet(cid: str, now=None) -> bool:
    """Let one key receive service keys again (FoodAssistant-xym8y).

    A Bandit put back on a version before 0.21 after its key was ratcheted
    gets the cut-off payload. Clearing the ratchet also forgets which of the
    key's devices sent relay1, so it sets again only once every device that
    pulls with the key has updated. Returns whether the key was ratcheted.
    Raises KeyError (unknown) or CredentialError (revoked)."""
    with _lock:
        rec = _records().get(cid or "")
        if rec is None:
            raise KeyError(cid)
        if rec.get("status") == "revoked":
            raise CredentialError("That key was revoked.")
        was = bool(rec.get("relay_capable_at"))

        def _m(recs):
            r = recs.get(cid)
            if r is None:
                r = copy.deepcopy(rec)
                recs[cid] = r
            r["relay_capable_at"] = None
            for p in r.get("pullers") or []:
                if isinstance(p, dict):
                    p["relay1_at"] = None
        _commit(_m, now=now)
        return was


def first_use(cid: str):
    rec = _records().get(cid or "")
    return rec.get("last_used") if rec else None


# --- Paired keys nobody used (FoodAssistant-x60df) -----------------------------

# A paired key unused this long after its approval is offered for removal in
# the inbox. It is never removed on its own: a device that is slow to finish
# setting up must not lose its key.
UNUSED_PAIRED_SECONDS = DAY


def never_used(rec: dict) -> bool:
    """No request with this key was ever served, or refused."""
    return not rec.get("last_used") and not rec.get("refusals")


def unused_paired_keys(now=None) -> list[dict]:
    """Active paired keys still unused a day or more after approval."""
    t = _now(now)
    out = []
    for rec in _records().values():
        if rec.get("kind") != "paired" or rec.get("status") != "active":
            continue
        if not never_used(rec) or _expired(rec, t):
            continue
        created = _ts(rec.get("created"))
        if created is None or t - created < UNUSED_PAIRED_SECONDS:
            continue
        out.append({"id": rec.get("id"), "name": rec.get("name") or "",
                    "created": rec.get("created")})
    out.sort(key=lambda r: r.get("created") or "")
    return out


def remove_if_never_used(cid: str, now=None) -> bool:
    """Revoke a paired key only while it has never been used.

    The inbox offer's Remove button calls this, so a device that connected
    after the offer appeared keeps its key. Returns True when the key is gone
    (now or already), False when it was kept. Raises KeyError (unknown) or
    CredentialError (not a paired key)."""
    with _lock:
        rec = _records().get(cid or "")
        if rec is None:
            raise KeyError(cid)
        if rec.get("kind") != "paired":
            raise CredentialError("Only a key from a device pairing can be removed here.")
        if rec.get("status") == "revoked":
            return True
        if not never_used(rec):
            return False
        revoke(cid, now=now)
        return True


def mark_holder_moved(legacy_cid: str, device_id: str) -> None:
    if not device_id:
        return

    def _m(recs):
        r = recs.get(legacy_cid)
        if r is None:
            return
        for h in r.get("holders") or []:
            if h.get("device_id") == device_id or h.get("key") == device_id:
                h["moved"] = True
    _commit(_m)


def exchange(legacy_record: dict, kind: str, holder_meta: dict | None = None, now=None) -> tuple[dict, str]:
    """Swap a legacy key held by Home Assistant for its own ha key."""
    meta = holder_meta or {}
    peer = str(meta.get("ip") or meta.get("peer") or "").strip()
    name = f"Home Assistant ({peer})" if peer else "Home Assistant"
    rec, secret = mint("ha", name, "exchanged", device_id=meta.get("device_id") or None,
                       created_by="exchange", now=now)
    hkey = _holder_key(str(meta.get("device_id") or ""), meta.get("client", ""),
                       meta.get("ua", ""), peer)
    lid = legacy_record.get("id")

    def _m(recs):
        r = recs.get(lid)
        if r is None:
            return
        for h in r.get("holders") or []:
            if h.get("key") == hkey:
                h["moved"] = True
    _commit(_m, now=now)
    return rec, secret


# --- Reading back ------------------------------------------------------------


def suggested_scope(cid: str, now=None) -> str | None:
    """The narrowest scope that covers everything this key did in 30 days."""
    from .access import allows_raw
    t = _now(now)
    rec = _records().get(cid or "")
    if not rec:
        return None
    used = [f for f in rec.get("families") or [] if (_ts(f.get("last")) or 0) >= t - 30 * DAY]
    if not used:
        return None
    for scope in ("display", "ha_rest", "ha", "satellite"):
        ok = True
        for f in used:
            for path in f.get("paths") or []:
                if not allows_raw(scope, f.get("method", "GET"), path, kiosk_is_admin=False):
                    ok = False
                    break
            if not ok:
                break
        if ok:
            if scope == rec.get("scope"):
                return None
            return scope
    return None


def display_name(cid: str) -> str:
    if cid == "agent:streamdeck":
        return "Stream Deck on this device"
    if cid == "agent:gadgets":
        return "Sensor reader on this device"
    rec = _records().get(cid or "")
    if rec and rec.get("kind") != "legacy":
        return rec.get("name") or "Key"
    s = _settings()
    if s.api_key and legacy_id(s.api_key) == cid:
        return "primary key"
    extras = s.extra_api_keys if isinstance(s.extra_api_keys, list) else []
    names = s.extra_api_key_names if isinstance(s.extra_api_key_names, list) else []
    for i, k in enumerate(extras):
        if isinstance(k, str) and k and legacy_id(k) == cid:
            label = names[i] if i < len(names) and isinstance(names[i], str) else ""
            return label.strip()[:60] or f"extra key {i + 1}"
    if rec and rec.get("source") == "env":
        return "primary key"
    return "unknown key"


# --- Hourly pass -------------------------------------------------------------


def _holder_is_fleet_device(h: dict) -> bool:
    if h.get("cub"):
        return True
    if str(h.get("device_mode") or "") == "pi_remote":
        return True
    client = str(h.get("client") or "").lower()
    ua = str(h.get("ua") or "").lower()
    return client.startswith(("satellite/", "cub")) or ua.startswith("esphome")


def retirement_pass(now=None) -> None:
    """Retire legacy keys nothing needs any more and warn about expiring admin keys."""
    from .access import raise_item
    t = _now(now)
    s = _settings()
    try:
        ensure_legacy_records(t)
    except Exception:
        pass
    changes: dict = {}
    revoke_ids: list = []
    expired_mirrors: list = []
    for rec in list(_records().values()):
        cid = rec.get("id")
        status = rec.get("status")
        if rec.get("kind") == "legacy":
            if rec.get("source") == "env" or status == "revoked":
                continue
            holders = rec.get("holders") or []
            all_moved = bool(holders) and all(h.get("moved") for h in holders)
            if all_moved and status == "active":
                changes[cid] = "retiring"
                status = "retiring"
            if status == "retiring" and s.legacy_key_auto_retire:
                last = _ts(rec.get("last_used")) or _ts(rec.get("created")) or t
                if t - last >= 90 * DAY:
                    if any(_holder_is_fleet_device(h) for h in holders):
                        raise_item(
                            "legacy_key_retire", "An older key is no longer used",
                            f"The key \"{display_name(cid)}\" has not been used for 90 days, "
                            "and every device that used it now has its own key. It was "
                            "used by a satellite or a Bandit Cub, so it was kept. Revoke "
                            "it in Settings, Security once you are sure nothing needs it.",
                            f"legacy_key_retire:{cid}")
                    else:
                        revoke_ids.append(cid)
        elif status != "revoked" and rec.get("mirrored") and _expired(rec, t):
            # An expired key stops working here at once; its copy for older
            # versions goes too, so the routes that still read extra_api_keys
            # (and a rollback) stop taking it.
            expired_mirrors.append(rec)
        elif rec.get("scope") == "admin" and status != "revoked":
            exp = _ts(rec.get("expires"))
            if exp is not None and exp - t <= 14 * DAY:
                raise_item(
                    "admin_key_expiry", "An admin key expires soon",
                    f"The admin key \"{rec.get('name') or 'Key'}\" stops working on "
                    f"{datetime.fromtimestamp(exp).date().isoformat()}. Create a new "
                    "admin key in Settings, Security and give it to the program that "
                    "uses this one.",
                    f"admin_key_expiry:{cid}")
    for rec in expired_mirrors:
        try:
            _remove_mirror(rec)
        except Exception:
            continue
        changes.setdefault(rec.get("id"), None)
    if changes:
        def _m(recs):
            for cid, st in changes.items():
                if cid not in recs:
                    continue
                if st is None:
                    recs[cid]["mirrored"] = False
                else:
                    recs[cid]["status"] = st
        _commit(_m, now=t)
    for cid in revoke_ids:
        name = display_name(cid)
        try:
            revoke(cid, now=t)
        except Exception:
            continue
        raise_item("legacy_key_retired", "An older key was removed",
                   f"The key \"{name}\" had not been used for 90 days, and every device "
                   "that used it has its own key now, so it was removed.",
                   f"legacy_key_retired:{cid}")
