"""LAN device pairing: a satellite asks this server for its own API key.

A new Pi Remote has no credentials yet, so its setup wizard can POST a pairing
request here instead of the user copying a key by hand. The request carries the
satellite's hostname; the server answers with a short numeric code that shows
on BOTH screens, and a logged-in user on the server confirms the codes match
and approves. Approval mints a named entry in ``extra_api_keys`` (the same
per-satellite key model the Security pane manages) and the satellite's status
poll picks the key up and fills its ``upstream_api_key``.

Security shape:
  - The request and status-poll endpoints are unauthenticated (the satellite
    has no key yet) but LAN-gated: only a private (RFC 1918 / loopback /
    link-local) client address may create or poll a request, and the whole
    feature turns off with the ``local_device_pairing_enabled`` setting.
  - Approve/deny are normal authenticated calls from the server's own UI.
  - Codes are 4 crypto-random digits, single-use, and expire after 5 minutes.
  - The request id (the poll handle) is a long random token, so only the
    device that created a request can read its key back.

Sharing: requests persist to a small state file under data_dir, the same
atomic-replace pattern as scanner_mode.py / ha_events.py, so a server running
multiple uvicorn workers sees one request list (the worker that took the POST
is rarely the one that serves the approve click). If data_dir is unwritable
(tests, a read-only mount) the module quietly degrades to process-local
in-memory state.
"""
from __future__ import annotations

import ipaddress
import json
import os
import secrets
import threading
import time
from pathlib import Path

# A pairing code lives this long; the satellite polls well inside it.
TTL_SECONDS = 300
# At most this many undecided requests at once: a hostile LAN box cannot fill
# the admin's screen with prompts, and a forgotten request expires on its own.
MAX_PENDING = 3

_lock = threading.Lock()
_requests: dict[str, dict] = {}
_mtime: int | None = None


def is_private_address(host: str) -> bool:
    """True when host is an address on the local network (or the host itself).

    Pairing hands out credentials, so it must never be reachable from the open
    internet: only RFC 1918 / loopback / link-local addresses qualify. A
    hostname (or anything unparsable) returns False; the check runs on
    ``request.client.host``, which is an IP for every real connection, so a
    non-IP value means a proxy or test harness we cannot vouch for.

    This looks only at the immediate peer, so it is NOT sufficient on its own
    behind a reverse proxy (the peer is then the proxy's private address). Gates
    on the unauthenticated pairing / Cub-firmware endpoints use
    ``is_local_network_request`` below, which also rejects proxied and tunneled
    requests (FoodAssistant-gbs8).
    """
    try:
        ip = ipaddress.ip_address((host or "").strip())
    except ValueError:
        return False
    return ip.is_private


def is_local_network_request(request) -> bool:
    """Whether an unauthenticated request truly originates on the LAN.

    Stricter than ``is_private_address``: a request that arrived through a
    reverse proxy or tunnel (whose immediate peer is the proxy's own private IP,
    or whose Host is the public tunnel hostname) is refused, so the pairing and
    Cub-firmware endpoints are not silently exposed to the internet when the app
    is fronted by Pangolin, Cloudflare, or the Forager tunnel (FoodAssistant-
    gbs8). A directly connected LAN client (private peer, no forwarding headers,
    non-public Host) still passes, so LAN pairing and Cub updates keep working.
    """
    from .request_origin import is_local_network, has_forwarding_headers
    from ..config import settings
    client = request.client.host if request.client else ""
    return is_local_network(
        request.headers.get("host", ""),
        settings.qr_public_url,
        settings.tunnel_enabled,
        client,
        has_forwarding_headers(request.headers),
    )


def _state_file() -> Path:
    # Resolved per call (not at import) so tests that repoint data_dir work.
    from ..config import settings
    return Path(settings.data_dir) / "pairing.json"


def _load_locked() -> None:
    """Refresh the in-process view from the state file if it changed on disk.
    Caller holds the lock."""
    global _requests, _mtime
    try:
        sf = _state_file()
        mtime = sf.stat().st_mtime_ns
    except OSError:
        return  # no file yet (fresh install, or unwritable data_dir)
    if mtime == _mtime:
        return
    try:
        data = json.loads(sf.read_text())
        reqs = {k: v for k, v in data.get("requests", {}).items()
                if isinstance(v, dict) and "code" in v}
    except (OSError, ValueError, TypeError, AttributeError):
        return  # a torn or corrupt file never breaks pairing; keep what we have
    _mtime = mtime
    _requests = reqs


def _save_locked() -> None:
    """Write the requests to the state file (atomic replace, best effort).
    Caller holds the lock."""
    global _mtime
    sf = _state_file()
    try:
        tmp = sf.with_name(sf.name + ".tmp")
        tmp.write_text(json.dumps({"requests": _requests}))
        os.replace(tmp, sf)
        _mtime = sf.stat().st_mtime_ns
    except OSError:
        pass  # data_dir not writable: fall back to process-local behavior


def _prune_locked(now: float) -> None:
    global _requests
    cutoff = now - TTL_SECONDS
    _requests = {k: v for k, v in _requests.items() if v.get("created", 0) >= cutoff}


def create_request(hostname: str, client_ip: str) -> dict | None:
    """Open a pairing request; returns {request_id, code} or None when the
    pending queue is full. The code is what both screens display."""
    now = time.time()
    with _lock:
        _load_locked()
        _prune_locked(now)
        pending = sum(1 for r in _requests.values() if r.get("status") == "pending")
        if pending >= MAX_PENDING:
            return None
        request_id = secrets.token_urlsafe(24)
        code = f"{secrets.randbelow(10000):04d}"
        _requests[request_id] = {
            "code": code,
            "hostname": str(hostname or "").strip()[:80],
            "ip": str(client_ip or ""),
            "created": now,
            "status": "pending",
        }
        _save_locked()
        return {"request_id": request_id, "code": code}


def get_status(request_id: str) -> dict:
    """The satellite's poll: pending, approved (with the minted key), denied,
    or expired (also the answer for an unknown id, so ids cannot be probed)."""
    now = time.time()
    with _lock:
        _load_locked()
        _prune_locked(now)
        rec = _requests.get(request_id)
        if rec is None:
            return {"status": "expired"}
        if rec["status"] == "approved":
            return {"status": "approved", "api_key": rec.get("api_key", ""),
                    "key_name": rec.get("key_name", "")}
        return {"status": rec["status"]}


def pending_requests() -> list[dict]:
    """Undecided requests for the server's Devices pane (no key material)."""
    now = time.time()
    with _lock:
        _load_locked()
        _prune_locked(now)
        return [
            {"request_id": rid, "code": r["code"], "hostname": r.get("hostname", ""),
             "ip": r.get("ip", ""), "expires_in": int(r.get("created", 0) + TTL_SECONDS - now)}
            for rid, r in _requests.items() if r.get("status") == "pending"
        ]


def approve(request_id: str, api_key: str, key_name: str = "") -> dict | None:
    """Mark a pending request approved, attaching the key the caller minted.

    Returns the record (hostname etc.) or None when the request is unknown,
    expired, or already decided (single-use: a code can never approve twice).
    """
    now = time.time()
    with _lock:
        _load_locked()
        _prune_locked(now)
        rec = _requests.get(request_id)
        if rec is None or rec.get("status") != "pending":
            return None
        rec["status"] = "approved"
        rec["api_key"] = api_key
        rec["key_name"] = key_name
        _save_locked()
        return dict(rec)


def deny(request_id: str) -> bool:
    """Mark a pending request denied. Unknown/decided requests return False."""
    now = time.time()
    with _lock:
        _load_locked()
        _prune_locked(now)
        rec = _requests.get(request_id)
        if rec is None or rec.get("status") != "pending":
            return False
        rec["status"] = "denied"
        _save_locked()
        return True


def reset() -> None:
    """Clear all requests and drop the state file (used by tests)."""
    global _requests, _mtime
    with _lock:
        _requests = {}
        _mtime = None
        try:
            _state_file().unlink(missing_ok=True)
        except OSError:
            pass
