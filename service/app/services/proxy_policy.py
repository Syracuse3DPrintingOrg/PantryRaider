"""Which Grocy and Mealie requests the satellite proxy passes along.

A satellite reaches its main server's Grocy and Mealie through /api/proxy
(routers/proxy.py), and the server adds its own credentials on the way. That
hop used to forward any path at all, so whoever held a satellite's API key held
the server's full Grocy and Mealie admin keys by proxy: user accounts, API key
management, Mealie's admin API.

This module decides what the hop may carry:

- validate_path() refuses a path that could be read two ways (dot segments,
  backslashes, control characters, a second layer of percent-encoding) or that
  is not under api/.
- classify() sorts a request into "allow" (a path some Pantry Raider release
  actually calls), "deny" (account, key and admin surfaces, refused in every
  mode) or "offlist" (anything else, passed along or refused depending on
  settings.proxy_path_policy).
- record() keeps a small tally of what came through, served to the owner at
  GET /admin/proxy-paths, so the list can be checked against real traffic
  before "enforce" becomes the default.

ALLOW is the union of the client paths of every release, including ones a
current release no longer calls (PUT api/objects/stock/ID up to v0.18.98, the
shopping_list_items table up to v0.17.7), because a satellite older than its
server must keep working. scripts/proxy-paths-history.sh repeats the scan of
release tags that built it.

The tables and the classifier are pure. The recorder follows the state-file
pattern (state_lock around the read-modify-write, temp file plus os.replace,
mode 0600, quiet in-memory fallback), but batches: satellites proxy a lot of
Grocy traffic, so counts build up in memory and reach the file at most every
30 seconds, or at once the first time this process sees a new path.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger("foodassistant.proxy")

CODE_INVALID = "proxy_path_invalid"
CODE_DENIED = "proxy_path_denied"
CODE_NOT_ALLOWED = "proxy_path_not_allowed"

VERDICT_ALLOW = "allow"
VERDICT_DENY = "deny"
VERDICT_OFFLIST = "offlist"
# Recorded only: a path validate_path refused never reaches classify().
VERDICT_INVALID = "invalid"

BACKEND_LABELS = {"grocy": "Grocy", "mealie": "Mealie"}


class ProxyPathInvalid(ValueError):
    """A proxied path the server will not interpret (400, proxy_path_invalid)."""


# --- Path validation ---------------------------------------------------------

_C0 = re.compile(r"[\x00-\x1f]")


def validate_path(path: str) -> list[str]:
    """The path's segments, or ProxyPathInvalid when it must not be forwarded.

    ``path`` is what Starlette hands the route, already percent-decoded once.
    A '%' left over means the client encoded it twice, which an upstream could
    decode again into something the checks below never saw. Pure.
    """
    if not isinstance(path, str) or not path:
        raise ProxyPathInvalid("empty path")
    if "\\" in path:
        raise ProxyPathInvalid("backslash in path")
    if _C0.search(path):
        raise ProxyPathInvalid("control character in path")
    if "%" in path:
        raise ProxyPathInvalid("percent sign left after decoding")
    segments = path.split("/")
    for seg in segments:
        if seg in ("", ".", ".."):
            raise ProxyPathInvalid("empty or dot segment")
    if segments[0] != "api" or len(segments) < 2:
        raise ProxyPathInvalid("path is not under api/")
    return segments


# RFC 3986 unreserved characters are always left alone by quote(); the
# sub-delims are the only other characters a rebuilt segment keeps as-is.
_SEGMENT_SAFE = "!$&'()*+,;="


def upstream_path(segments: list[str]) -> str:
    """Rebuild a validated path for the upstream URL, each segment re-encoded
    on its own so a barcode with a space (or any other character) arrives as
    one segment and never as extra path. Pure."""
    return "/".join(quote(seg, safe=_SEGMENT_SAFE) for seg in segments)


# --- Tables --------------------------------------------------------------------
# Placeholders in a template: ID is decimal digits, SEG is any one segment,
# SLUG is [A-Za-z0-9._~-]+, FILE is a recipe image file name. {a,b} expands to
# one entry per alternative.

_GROCY_OBJECTS_READ = ("locations,product_groups,products,quantity_units,"
                       "quantity_unit_conversions,shopping_lists,shopping_list,"
                       "product_barcodes,stock,stock_log")
_GROCY_OBJECTS_CREATE = ("locations,product_groups,quantity_units,products,"
                         "product_barcodes,shopping_lists,shopping_list")

_GROCY_ALLOW = [
    ("GET", "api/stock"),
    ("GET", "api/system/info"),
    ("GET", "api/stock/products/ID/entries"),
    ("GET", "api/objects/{%s}" % _GROCY_OBJECTS_READ),
    ("POST", "api/objects/{%s}" % _GROCY_OBJECTS_CREATE),
    ("POST", "api/stock/products/ID/{add,consume,open,inventory,transfer}"),
    ("POST", "api/stock/products/by-barcode/SEG/consume"),
    ("PUT", "api/objects/{products,shopping_list}/ID"),
    ("PUT", "api/stock/entry/ID"),
    ("DELETE", "api/objects/shopping_list/ID"),
    # Satellites v0.17.8 to v0.19.6 read one list row before updating it.
    ("GET", "api/objects/shopping_list/ID"),
    # Satellites up to v0.18.98 moved a stock entry through the generic API.
    ("PUT", "api/objects/stock/ID"),
    # Satellites up to v0.17.7 used this table name for the shopping list.
    ("GET", "api/objects/shopping_list_items"),
    ("GET", "api/objects/shopping_list_items/ID"),
    ("POST", "api/objects/shopping_list_items"),
    ("PUT", "api/objects/shopping_list_items/ID"),
    ("DELETE", "api/objects/shopping_list_items/ID"),
]

_MEALIE_ALLOW = [
    ("GET", "api/users/self"),
    ("GET", "api/recipes"),
    ("GET", "api/recipes/SLUG"),
    ("GET", "api/foods"),
    ("GET", "api/units"),
    ("GET", "api/media/recipes/SEG/images/FILE"),
    ("POST", "api/{recipes,foods,units}"),
    ("POST", "api/recipes/create/url"),
    ("POST", "api/recipes/create-url"),
    ("PATCH", "api/recipes/SLUG"),
    # Mealie v2 scopes these under households/, v1 under groups/.
    ("GET", "api/{households,groups}/{mealplans,shopping/lists}"),
    ("GET", "api/{households,groups}/shopping/lists/SEG"),
    ("POST", "api/{households,groups}/{mealplans,shopping/items}"),
    ("PUT", "api/{households,groups}/shopping/items/SEG"),
    ("DELETE", "api/{households,groups}/{mealplans,shopping/items}/SEG"),
]

# Refused in every mode, whatever the policy says: accounts, API keys,
# sessions, files, and admin. Grocy's system/ is closed apart from system/info,
# and Mealie's groups/ and households/ apart from the meal plan and shopping.
# Case is ignored and a name ends at any punctuation, not only at '/', so
# "api/Users" or "api/users;x" cannot slip past as a different path. The
# exceptions (system/info, users/self, mealplans, shopping) match exact case
# only, so "api/users/SELF" is refused rather than let through. Grocy's
# calendar sharing link is here because asking for it creates an API key.
_DENY_END = r"(?=[^A-Za-z0-9_]|$)"
_HARD_DENY = {
    "grocy": re.compile(
        r"^api/(users|user|system/(?!(?-i:info)$)[^/]*|files"
        r"|calendar/ical/sharing-link"
        r"|objects/(api_keys|users|user_permissions|permission[^/]*|sessions))"
        + _DENY_END, re.IGNORECASE),
    "mealie": re.compile(
        r"^api/(admin|auth|users(?!(?-i:/self)$)"
        r"|groups/(?!(?-i:mealplans|shopping)(/|$))[^/]*"
        r"|households/(?!(?-i:mealplans|shopping)(/|$))[^/]*"
        r"|organizers/.*/delete)" + _DENY_END, re.IGNORECASE),
}

_ID = re.compile(r"^[0-9]+$")
_SLUG = re.compile(r"^[A-Za-z0-9._~-]+$")
_FILE = re.compile(r"^[A-Za-z0-9._-]+\.(webp|jpg|jpeg|png)$")


def _expand(template: str) -> list[str]:
    """Expand the first {a,b,...} group (recursively) into plain templates."""
    m = re.search(r"\{([^{}]*)\}", template)
    if not m:
        return [template]
    out = []
    for alt in m.group(1).split(","):
        out.extend(_expand(template[:m.start()] + alt + template[m.end():]))
    return out


def _compile(table) -> dict[str, list[tuple[str, list[str]]]]:
    by_method: dict[str, list[tuple[str, list[str]]]] = {}
    for method, raw in table:
        for tpl in _expand(raw):
            by_method.setdefault(method, []).append((tpl, tpl.split("/")))
    return by_method


_ALLOW = {"grocy": _compile(_GROCY_ALLOW), "mealie": _compile(_MEALIE_ALLOW)}


def allow_templates(backend: str) -> list[tuple[str, str]]:
    """Every (method, template) the proxy passes along for ``backend``. Pure."""
    return [(m, tpl) for m, rows in _ALLOW.get(backend, {}).items() for tpl, _ in rows]


def _token_matches(token: str, seg: str) -> bool:
    if token == "ID":
        return bool(_ID.match(seg))
    if token == "SEG":
        return True
    if token == "SLUG":
        return bool(_SLUG.match(seg))
    if token == "FILE":
        return bool(_FILE.match(seg))
    return token == seg


def generic_template(segments: list[str]) -> str:
    """A bounded template for a path on no list: numbers become ID, and an
    unusual or long segment becomes SEG, so the recorder never stores a
    barcode, a name, or an unbounded number of distinct paths. Pure."""
    out = []
    for seg in segments[:12]:
        if _ID.match(seg):
            out.append("ID")
        elif len(seg) > 40 or not _SLUG.match(seg):
            out.append("SEG")
        else:
            out.append(seg)
    if len(segments) > 12:
        out.append("...")
    return "/".join(out)


def classify(backend: str, method: str, path: str) -> tuple[str, str]:
    """('allow' | 'deny' | 'offlist', template) for one proxied request.

    ``path`` must already have passed validate_path. Deny is checked first so
    no allow entry can ever open a denied surface. Pure.
    """
    method = (method or "").upper()
    segments = path.split("/")
    deny = _HARD_DENY.get(backend)
    if deny is not None and deny.search(path):
        return VERDICT_DENY, generic_template(segments)
    for tpl, tokens in _ALLOW.get(backend, {}).get(method, []):
        if len(tokens) == len(segments) and all(
                _token_matches(t, s) for t, s in zip(tokens, segments)):
            return VERDICT_ALLOW, tpl
    return VERDICT_OFFLIST, generic_template(segments)


def enforcing(policy) -> bool:
    """True only for an explicit 'enforce'; anything else reports. Pure."""
    return str(policy or "").strip().lower() == "enforce"


# --- User-facing refusals ------------------------------------------------------

def not_allowed_detail(backend: str, method: str, path: str) -> str:
    label = BACKEND_LABELS.get(backend, backend)
    return (f"The main server does not pass this {label} request along "
            f"({method.upper()} /{path}). Update this device and the main "
            "server to the same version.")


def denied_detail(backend: str, method: str, path: str) -> str:
    label = BACKEND_LABELS.get(backend, backend)
    return (f"The main server does not pass this {label} request along "
            f"({method.upper()} /{path}). Accounts, keys and admin settings "
            f"are managed in {label} on the main server itself.")


def invalid_detail(backend: str) -> str:
    label = BACKEND_LABELS.get(backend, backend)
    return (f"The main server could not read the address of this {label} "
            "request, so it did not pass it along.")


def refusal_detail(status_code: int, body_text: str) -> str | None:
    """The server's own words when a proxy answer is a path refusal, else None.

    Satellite clients call this on an error status so the kiosk banner shows
    what the main server said instead of a bare "Grocy 403". Pure.
    """
    if status_code not in (400, 403) or not body_text:
        return None
    try:
        data = json.loads(body_text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    code = data.get("code")
    if not isinstance(code, str) or not code.startswith("proxy_path_"):
        return None
    detail = data.get("detail")
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    return "The main server did not pass this request along."


# --- Recorder ------------------------------------------------------------------

FLUSH_INTERVAL = 30.0
MAX_ENTRIES = 500      # distinct (backend, method, template, verdict) rows
MAX_TALLY = 50         # distinct credentials or clients kept per row
MAX_KNOWN = 2 * MAX_ENTRIES  # rows remembered for the write-at-once rule
_OVERFLOW = "(other)"

_lock = threading.Lock()
_pending: dict[str, dict] = {}
_known: set[str] = set()
_known_dir: str | None = None
_last_flush = 0.0
_cache: dict | None = None
_cache_mtime: float | None = None
_cache_path: str | None = None
_now = time.time


def _state_file() -> Path:
    from ..config import settings
    return Path(settings.data_dir) / "proxy_paths.json"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _key(backend, method, template, verdict) -> str:
    return f"{backend} {method} {template} {verdict}"


def _read_file(path: Path) -> dict:
    """The recorded tally on disk, mtime-cached. {} when absent or unreadable."""
    global _cache, _cache_mtime, _cache_path
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if _cache is not None and _cache_path == str(path) and _cache_mtime == mtime:
        return _cache
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("paths"), dict):
        data = {"paths": {}}
    _cache, _cache_mtime, _cache_path = data, mtime, str(path)
    return data


def _bump(tally: dict, name: str, n: int) -> None:
    if name not in tally and len(tally) >= MAX_TALLY:
        name = _OVERFLOW
    tally[name] = int(tally.get(name, 0)) + n


def _merge_row(into: dict, row: dict) -> None:
    into["count"] = int(into.get("count", 0)) + int(row.get("count", 0))
    firsts = [t for t in (into.get("first_seen"), row.get("first_seen")) if t]
    lasts = [t for t in (into.get("last_seen"), row.get("last_seen")) if t]
    into["first_seen"] = min(firsts) if firsts else None
    into["last_seen"] = max(lasts) if lasts else None
    for field in ("credentials", "clients"):
        tally = into.setdefault(field, {})
        for name, n in (row.get(field) or {}).items():
            _bump(tally, name, int(n))


def _merge(paths: dict, pending: dict) -> dict:
    out = {k: dict(v, credentials=dict(v.get("credentials") or {}),
                   clients=dict(v.get("clients") or {}))
           for k, v in paths.items() if isinstance(v, dict)}
    for key, row in pending.items():
        if key not in out:
            if len(out) >= MAX_ENTRIES:
                continue
            out[key] = {f: row[f] for f in ("backend", "method", "template", "verdict")}
        _merge_row(out[key], row)
    return out


def _flush_locked(path: Path) -> bool:
    """Write the pending counts to disk. Caller holds _lock. True on success."""
    global _pending, _last_flush, _cache, _cache_mtime
    from .state_lock import state_write_lock
    _last_flush = _now()
    if not _pending:
        return True
    try:
        with state_write_lock(path):
            _cache = None  # read fresh under the lock: another worker may have written
            on_disk = _read_file(path)
            merged = _merge(on_disk.get("paths") or {}, _pending)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps({"paths": merged}, indent=1, sort_keys=True))
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
            _cache, _cache_mtime = None, None
    except OSError as exc:
        # Unwritable data_dir: the counts stay in memory and still show at
        # /admin/proxy-paths for this process.
        logger.debug("proxy paths: could not write %s: %s", path, exc)
        return False
    _pending = {}
    return True


def _load_known_locked(path: Path) -> None:
    global _known, _known_dir
    if _known_dir == str(path):
        return
    _known = set((_read_file(path).get("paths") or {}).keys())
    _known_dir = str(path)


def record(backend: str, method: str, template: str, credential_name: str,
           client: str, verdict: str) -> None:
    """Count one proxied request. Never raises.

    The write reaches disk at once the first time this process sees a
    (backend, method, template, verdict), and otherwise at most every
    FLUSH_INTERVAL seconds, so steady traffic costs no file write per request.
    """
    try:
        path = _state_file()
        now = _now()
        key = _key(backend, method, template, verdict)
        with _lock:
            _load_known_locked(path)
            row = _pending.get(key)
            if row is None:
                if len(_pending) >= MAX_ENTRIES:
                    return
                row = _pending[key] = {
                    "backend": backend, "method": method, "template": template,
                    "verdict": verdict, "count": 0, "first_seen": _iso(now),
                    "last_seen": None, "credentials": {}, "clients": {}}
            row["count"] += 1
            row["last_seen"] = _iso(now)
            _bump(row["credentials"], credential_name or "unknown", 1)
            _bump(row["clients"], client or "unknown", 1)
            # Only the first MAX_KNOWN distinct rows get an immediate write, so
            # a client inventing new paths cannot force a write per request.
            new = key not in _known and len(_known) < MAX_KNOWN
            if new:
                _known.add(key)
            if new or now - _last_flush >= FLUSH_INTERVAL:
                _flush_locked(path)
    except Exception as exc:  # the tally must never break a proxied request
        logger.debug("proxy paths: record failed: %s", exc)


def flush() -> None:
    """Write any pending counts now. Never raises."""
    try:
        path = _state_file()
        with _lock:
            _flush_locked(path)
    except Exception as exc:
        logger.debug("proxy paths: flush failed: %s", exc)


def snapshot() -> list[dict]:
    """Every recorded row, disk plus anything not yet written, busiest first
    within each verdict (refused and off-list rows lead)."""
    path = _state_file()
    with _lock:
        _flush_locked(path)
        merged = _merge((_read_file(path).get("paths") or {}), _pending)
    order = {VERDICT_DENY: 0, VERDICT_INVALID: 1, VERDICT_OFFLIST: 2, VERDICT_ALLOW: 3}
    rows = list(merged.values())
    rows.sort(key=lambda r: (order.get(r.get("verdict"), 4), -int(r.get("count", 0)),
                             r.get("backend", ""), r.get("template", "")))
    return rows


def reset() -> None:
    """Forget in-memory state (tests)."""
    global _pending, _known, _known_dir, _last_flush, _cache, _cache_mtime, _cache_path
    with _lock:
        _pending, _known, _known_dir = {}, set(), None
        _last_flush = 0.0
        _cache = _cache_mtime = _cache_path = None
