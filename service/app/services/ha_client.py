"""The one place this app talks to Home Assistant for its own actions.

Every Home Assistant call the app makes for itself (a deck or screen key, an
ESP button, the camera proxy, the probe-alert notification, the daily token
checks) goes through here, so the rules live in one module:

- mode() says how Home Assistant is reached: 'direct' with this install's own
  address and token, 'upstream' on a satellite whose main server relays Home
  Assistant for it (through the server's /api/proxy/ha), or 'none'.
- call_service() checks the key policy (services/ha_policy.py) before any
  request, and sends the normalized entity and service the way Home Assistant
  itself reads them, with every URL part quoted.
- Nothing returned or logged ever carries the token, a URL with credentials,
  or exception text that could hold them.

A small state file (data_dir/ha-status.json) keeps when the token was last
checked, when the weekly owner-token notification last went out, and the last
probe-alert outcome, using the shared state-file pattern (the state_lock
write lock around the read-modify-write, temp file plus os.replace, 0600, mtime-cached reads, and an
in-memory fallback when data_dir is unwritable).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

from ..config import settings
from .state_lock import state_write_lock
from .ttl_cache import TTLCache

log = logging.getLogger(__name__)

NOT_CONFIGURED_MSG = "Home Assistant is not configured (Settings, Home Assistant)."
REFUSED_MSG = ("Home Assistant refused this action. Reload the Pantry Raider "
               "integration in Home Assistant so its account can control this device.")
UNREACHABLE_MSG = "Could not reach Home Assistant."
PROBE_ALERT_UPDATE_MSG = ("Probe alerts need the updated Pantry Raider integration "
                          "in Home Assistant.")

# The notification an owner-account token earns once a week (only for a token
# the integration handed over from the owner, never for a pasted one).
LIMITED_ACCOUNT_NOTICE_ID = "pantry_raider_limited_account"
LIMITED_ACCOUNT_NOTICE = (
    "Pantry Raider is using your owner account to reach Home Assistant. Update "
    "the Pantry Raider integration in HACS and approve its pairing on the "
    "kitchen screen or in Pantry Raider under Settings, Bandit Remotes, and it "
    "switches to a limited account of its own.")

MAX_STATES = 50
STATE_CACHE_SECONDS = 2.0
EXPIRES_SOON_DAYS = 14
CHECK_EVERY_SECONDS = 24 * 3600
# An unreachable Home Assistant is asked again an hour later, not every pass.
CHECK_RETRY_SECONDS = 3600
NOTIFY_EVERY_SECONDS = 7 * 24 * 3600

_TIMEOUT = 6.0


class HaNotDirect(Exception):
    """Raised by the direct-only calls when this install has no Home
    Assistant connection of its own."""


# --------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------

def _direct_connection() -> tuple[str, str]:
    base = str(settings.streamdeck_ha_base_url or "").strip().rstrip("/")
    token = str(settings.streamdeck_ha_token or "").strip()
    return base, token


def mode() -> str:
    """'direct', 'upstream' or 'none'."""
    try:
        if settings.is_satellite() and settings.fleet_relay_active("ha"):
            relay = settings.fleet_relay if isinstance(settings.fleet_relay, dict) else {}
            return "upstream" if relay.get("ha_available") else "none"
    except Exception:  # noqa: BLE001 - a malformed relay block reads as local
        pass
    base, token = _direct_connection()
    return "direct" if base and token else "none"


def _direct_client(timeout: float = _TIMEOUT) -> httpx.AsyncClient:
    """The SSRF-guarded client for Home Assistant on the LAN."""
    from . import egress
    return egress.guarded_async_client(allow_private=True, timeout=timeout)


def _upstream_client(timeout: float = _TIMEOUT) -> httpx.AsyncClient:
    """A client for the main server (the satellite's own configured server)."""
    return httpx.AsyncClient(timeout=timeout)


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _upstream_url(path: str) -> str:
    remote = str(settings.remote_server_url or "").strip().rstrip("/")
    return f"{remote}/api/proxy/ha/{path}"


def _upstream_kwargs() -> dict:
    from . import upstream
    return upstream.request_kwargs()


def _require_direct() -> tuple[str, str]:
    if mode() != "direct":
        raise HaNotDirect()
    return _direct_connection()


def _quiet(what: str, exc: BaseException) -> None:
    # The exception type only: httpx messages can carry the URL.
    log.debug("Home Assistant %s failed (%s)", what, type(exc).__name__)


# --------------------------------------------------------------------------
# States
# --------------------------------------------------------------------------

class _EntityCache(TTLCache):
    """Per-entity state cache. A TTLCache subclass so the test suite's cache
    reset (which invalidates every TTLCache it finds) empties it too."""

    def __init__(self, ttl: float) -> None:
        super().__init__(ttl)
        self.entries: dict = {}

    def lookup(self, key, now: float):
        hit = self.entries.get(key)
        if hit and now - hit[0] < self.ttl:
            return True, hit[1]
        return False, None

    def store(self, key, value, now: float) -> None:
        if len(self.entries) > 4 * MAX_STATES:
            self.entries.clear()
        self.entries[key] = (now, value)

    def invalidate(self) -> None:
        super().invalidate()
        self.entries.clear()


_state_cache = _EntityCache(STATE_CACHE_SECONDS)


def _state_path(entity_id: str) -> str:
    return "api/states/" + quote(str(entity_id or "").strip(), safe="")


async def _fetch_state(client, entity_id: str, m: str) -> dict | None:
    path = _state_path(entity_id)
    if m == "direct":
        base, token = _direct_connection()
        r = await client.get(f"{base}/{path}", headers=_bearer(token))
    else:
        r = await client.get(_upstream_url(path), **_upstream_kwargs())
    if r.status_code != 200:
        return None
    body = r.json()
    return body if isinstance(body, dict) else None


async def get_state(entity_id: str) -> dict | None:
    """One entity's state from Home Assistant, or None."""
    states = await get_states([entity_id])
    return states.get(str(entity_id or "").strip())


async def get_states(ids) -> dict:
    """{entity_id: state} for at most MAX_STATES ids; missing or unreadable
    entities are left out. Answers are cached for two seconds per entity."""
    m = mode()
    if m == "none":
        return {}
    wanted: list[str] = []
    for raw in ids or []:
        ent = str(raw or "").strip()
        if ent and ent not in wanted:
            wanted.append(ent)
        if len(wanted) >= MAX_STATES:
            break
    now = time.monotonic()
    out: dict = {}
    todo: list[str] = []
    for ent in wanted:
        hit, value = _state_cache.lookup((m, ent), now)
        if hit:
            if value is not None:
                out[ent] = value
        else:
            todo.append(ent)
    if not todo:
        return out
    try:
        async with (_direct_client() if m == "direct" else _upstream_client()) as client:
            for ent in todo:
                try:
                    value = await _fetch_state(client, ent, m)
                except Exception as exc:  # noqa: BLE001 - one entity never stops the rest
                    _quiet("state read", exc)
                    continue
                _state_cache.store((m, ent), value, now)
                if value is not None:
                    out[ent] = value
    except Exception as exc:  # noqa: BLE001
        _quiet("state read", exc)
    return out


async def list_states(base: str | None = None, token: str | None = None,
                      client=None) -> list:
    """Every entity state from Home Assistant (GET /api/states).

    Direct only: raises HaNotDirect when this install has no connection of its
    own and no address and token were passed in. ``base`` and ``token``
    override the saved ones (the Cameras page tests a typed token before it is
    saved). Non-200 answers, a non-list body and connection failures give []."""
    if base and token:
        base = str(base).strip().rstrip("/")
        token = str(token).strip()
    else:
        saved_base, saved_token = _require_direct()
        base = str(base or saved_base).strip().rstrip("/")
        token = str(token or saved_token).strip()
    own = client is None
    try:
        if own:
            client = _direct_client(timeout=8.0)
        r = await client.get(f"{base}/api/states",
                             headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            return []
        rows = r.json()
    except Exception as exc:  # noqa: BLE001 - an unreachable HA degrades
        _quiet("state list", exc)
        return []
    finally:
        if own and client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass
    return rows if isinstance(rows, list) else []


# --------------------------------------------------------------------------
# Services
# --------------------------------------------------------------------------

def _answer_detail(status: int, service: str, upstream_body=None) -> dict:
    if 200 <= status < 300:
        return {"ok": True, "status": status, "detail": "Sent " + service}
    if upstream_body is not None:
        # The main server's own refusal (for example an action it does not
        # allow) is our copy, never Home Assistant's, so it is safe to show.
        detail = upstream_body.get("detail") if isinstance(upstream_body, dict) else None
        if isinstance(detail, str) and detail and upstream_body.get("code"):
            return {"ok": False, "status": status, "detail": detail[:200]}
    if status in (401, 403):
        return {"ok": False, "status": status, "detail": REFUSED_MSG}
    return {"ok": False, "status": status, "detail": f"Home Assistant answered {status}."}


async def call_service(entity_id, service, *, source, entry=None) -> dict:
    """Run one Home Assistant service on an entity for a key press.

    Returns {'ok', 'status', 'detail'}. The key policy is checked first and a
    refusal makes no request at all."""
    from . import ha_allowlist, ha_policy
    allowed, msg = ha_policy.permitted(entity_id, service, entry, source,
                                       enforce=ha_allowlist.policy_enforced(settings))
    if not allowed:
        return {"ok": False, "status": 0, "detail": msg}
    m = mode()
    if m == "none":
        return {"ok": False, "status": 0, "detail": NOT_CONFIGURED_MSG}
    entity, svc = ha_policy.normalize(entity_id, service)
    domain, name = svc.split(".", 1)
    path = f"api/services/{quote(domain, safe='')}/{quote(name, safe='')}"
    body = {"entity_id": entity}
    try:
        if m == "direct":
            base, token = _direct_connection()
            async with _direct_client() as client:
                r = await client.post(f"{base}/{path}", headers=_bearer(token), json=body)
            return _answer_detail(r.status_code, svc)
        async with _upstream_client() as client:
            r = await client.post(_upstream_url(path), json=body, **_upstream_kwargs())
        reply = None
        if r.status_code >= 300:
            try:
                reply = r.json()
            except Exception:  # noqa: BLE001
                reply = None
        return _answer_detail(r.status_code, svc, reply if isinstance(reply, dict) else None)
    except Exception as exc:  # noqa: BLE001 - never leak the URL or token
        _quiet("service call", exc)
        return {"ok": False, "status": 0, "detail": UNREACHABLE_MSG}


async def call_raw_service(domain: str, service: str, data: dict) -> int:
    """POST one of the app's own fixed service calls (a notification, the
    probe alert). Direct only. Returns the HTTP status, 0 when unreachable."""
    base, token = _require_direct()
    path = f"api/services/{quote(str(domain), safe='')}/{quote(str(service), safe='')}"
    try:
        async with _direct_client() as client:
            r = await client.post(f"{base}/{path}", headers=_bearer(token), json=data or {})
        return int(r.status_code)
    except Exception as exc:  # noqa: BLE001
        _quiet("service call", exc)
        return 0


async def camera_snapshot(entity_id: str) -> tuple[int, bytes, str]:
    """(status, content, content_type) of a camera entity's picture. Direct
    only; (0, b'', '') when Home Assistant cannot be reached."""
    base, token = _require_direct()
    path = "api/camera_proxy/" + quote(str(entity_id or "").strip(), safe="")
    try:
        async with _direct_client(timeout=10.0) as client:
            r = await client.get(f"{base}/{path}", headers=_bearer(token))
        return (int(r.status_code), bytes(r.content or b""),
                str(r.headers.get("content-type", "")))
    except Exception as exc:  # noqa: BLE001
        _quiet("camera picture", exc)
        return 0, b"", ""


async def ping() -> bool:
    """True when Home Assistant answers GET /api/ with this token."""
    try:
        base, token = _require_direct()
        async with _direct_client(timeout=4.0) as client:
            r = await client.get(f"{base}/api/", headers=_bearer(token))
        return r.status_code == 200
    except Exception as exc:  # noqa: BLE001
        _quiet("ping", exc)
        return False


async def is_admin_token() -> bool | None:
    """Whether the token's user is a Home Assistant administrator.

    The template API is admin-only, so 200 means admin and 401 or 403 means
    a limited account. Anything else (unreachable, an odd answer) is None.

    Home Assistant counts every 401 as a failed sign-in from this address: it
    shows a 'Login attempt failed' notification and, where a sign-in limit is
    set, bans the address once the limit is reached. So this is asked once
    per token, never for the integration's limited account (see
    maybe_run_daily_checks)."""
    try:
        base, token = _require_direct()
        async with _direct_client(timeout=6.0) as client:
            r = await client.post(f"{base}/api/template", headers=_bearer(token),
                                  json={"template": "{{ 1 }}"})
    except Exception as exc:  # noqa: BLE001
        _quiet("admin check", exc)
        return None
    if r.status_code == 200:
        return True
    if r.status_code in (401, 403):
        return False
    return None


def _claims(token: str | None) -> dict:
    """The token's JWT claims, read without checking the signature, or {}."""
    raw = str(token if token is not None else (settings.streamdeck_ha_token or "")).strip()
    parts = raw.split(".")
    if len(parts) != 3 or not parts[1]:
        return {}
    try:
        seg = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(seg.encode("ascii")))
    except Exception:  # noqa: BLE001 - not a JWT we can read
        return {}
    return claims if isinstance(claims, dict) else {}


def token_expiry(token: str | None = None) -> str:
    """The token's expiry as an ISO date (UTC), read from its JWT exp claim
    without checking the signature. '' when it is not a JWT or has no exp."""
    exp = _claims(token).get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return ""
    try:
        return datetime.fromtimestamp(float(exp), tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def token_ref(token: str | None = None) -> str:
    """A short digest naming which Home Assistant token this install holds.

    A Home Assistant access token is a JWT whose iss claim is the id of the
    refresh token it belongs to. The integration hashes the ids of the tokens
    it made the same way (sha256, first 16 hex characters), so it can tell
    whether this install still holds one it has since removed. The digest
    reveals neither the token nor the id. '' when the token is not a JWT."""
    iss = _claims(token).get("iss")
    if not isinstance(iss, str) or not iss:
        return ""
    return hashlib.sha256(iss.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# Status file
# --------------------------------------------------------------------------

_mem: dict = {"path": None, "mtime": None, "data": {}}


def _status_file() -> Path:
    return Path(settings.data_dir) / "ha-status.json"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _load_status() -> dict:
    sf = _status_file()
    if _mem["path"] != str(sf):
        _mem.update(path=str(sf), mtime=None, data={})
    try:
        mtime = sf.stat().st_mtime_ns
    except OSError:
        return dict(_mem["data"])
    if mtime != _mem["mtime"]:
        try:
            data = json.loads(sf.read_text())
            if isinstance(data, dict):
                _mem["data"] = data
                _mem["mtime"] = mtime
        except (OSError, ValueError):
            pass
    return dict(_mem["data"])


def _write_status(data: dict) -> None:
    sf = _status_file()
    _mem["path"] = str(sf)
    _mem["data"] = dict(data)
    try:
        tmp = sf.with_name(sf.name + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, sf)
        _mem["mtime"] = sf.stat().st_mtime_ns
    except OSError:
        pass  # unwritable data_dir: keep it in memory


def _update_status(**changes) -> dict:
    with state_write_lock(_status_file()):
        data = _load_status()
        for key, value in changes.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        _write_status(data)
        return data


def record_probe_alert(ok: bool, detail: str = "") -> None:
    """Remember how the last probe alert went (shown on the Home Assistant pane)."""
    try:
        _update_status(probe_alert={"ok": bool(ok), "at": _iso(time.time()),
                                    "detail": str(detail or "")[:200]})
    except Exception:  # noqa: BLE001 - status bookkeeping never breaks an alert
        log.debug("could not record the probe alert outcome", exc_info=True)


def invalidate_checks() -> None:
    """Forget the last token check so the next pass checks again."""
    try:
        _update_status(checked_at=None)
    except Exception:  # noqa: BLE001
        log.debug("could not clear the token check time", exc_info=True)


def _token_fingerprint(token: str | None = None) -> str:
    """A short digest of the saved token, kept in ha-status.json so the admin
    check runs once per token rather than once a day."""
    raw = str(token if token is not None else (settings.streamdeck_ha_token or "")).strip()
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _expires_soon(expires: str, now: float) -> bool:
    if not expires:
        return False
    try:
        day = datetime.fromisoformat(str(expires)[:10]).replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return day.timestamp() - now <= EXPIRES_SOON_DAYS * 86400


def status_snapshot(now: float | None = None) -> dict:
    """What the Home Assistant pane shows about the token and probe alerts."""
    now = time.time() if now is None else now
    data = _load_status()
    probe = data.get("probe_alert")
    expires = str(settings.ha_token_expires or "")
    return {
        "kind": settings.ha_token_kind,
        "admin": settings.ha_token_admin,
        "expires": expires,
        "expires_soon": _expires_soon(expires, now),
        "probe_alert": dict(probe) if isinstance(probe, dict) else None,
        "checked_at": data.get("checked_at"),
    }


async def maybe_run_daily_checks(now: float | None = None) -> None:
    """Once a day, read when the token expires and nudge the owner weekly
    while the integration still uses the owner account. Whether the token is
    an administrator's is asked once per token (never for the integration's
    limited account), since Home Assistant counts a limited account's 401 as
    a failed sign-in. Only on a non-satellite install that holds its own
    connection. Never raises."""
    try:
        if settings.is_satellite() or mode() != "direct":
            return
        now = time.time() if now is None else now
        data = _load_status()
        last = _epoch(data.get("checked_at"))
        if last is not None and now - last < CHECK_EVERY_SECONDS:
            return
        kind = str(settings.ha_token_kind or "")
        fingerprint = _token_fingerprint()
        status_changes: dict = {}
        if kind == "dedicated":
            # The integration only ever hands over a limited account, and
            # asking would cost a failed sign-in in Home Assistant's log.
            admin = False
        elif (settings.ha_token_admin is not None
              and data.get("admin_checked") == fingerprint):
            admin = settings.ha_token_admin
        else:
            admin = await is_admin_token()
            if admin is not None:
                status_changes["admin_checked"] = fingerprint
        expires = token_expiry()
        changes: dict = {}
        if admin is not None and settings.ha_token_admin != admin:
            changes["ha_token_admin"] = admin
        if str(settings.ha_token_expires or "") != expires:
            changes["ha_token_expires"] = expires
        if changes:
            settings.save(changes)
        checked = now if admin is not None else now - CHECK_EVERY_SECONDS + CHECK_RETRY_SECONDS
        _update_status(checked_at=_iso(checked), **status_changes)
        if (settings.ha_token_kind == "owner-legacy" and admin is True):
            notified = _epoch(data.get("notified_at"))
            if notified is None or now - notified >= NOTIFY_EVERY_SECONDS:
                status = await call_raw_service("persistent_notification", "create", {
                    "title": "Pantry Raider",
                    "notification_id": LIMITED_ACCOUNT_NOTICE_ID,
                    "message": LIMITED_ACCOUNT_NOTICE,
                })
                if 200 <= status < 300:
                    _update_status(notified_at=_iso(now))
    except Exception:  # noqa: BLE001 - a background check never breaks the poll
        log.debug("Home Assistant daily checks failed", exc_info=True)
