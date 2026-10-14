"""LAN device pairing: a satellite asks this server for its own API key.

A new Pi Remote has no credentials yet, so its setup wizard can POST a pairing
request here instead of the user copying a key by hand. The request carries the
satellite's hostname; the server answers with a short numeric code that shows
on BOTH screens, and a logged-in user on the server confirms the codes match
and approves. Approval mints a key of its own for the device, limited to what
that kind of device needs (services/credentials.py), and the device's status
poll picks the key up (a satellite fills its ``upstream_api_key``). The poll
hands the key out until the device first uses it, or for two minutes after
the poll that first returned it, and then answers without it.

Security shape:
  - The request and status-poll endpoints are unauthenticated (the satellite
    has no key yet) but LAN-gated: only a device on the home network (by the
    access rules, which include the Home networks setting) may create or
    poll a request, and the whole feature turns off with the
    ``local_device_pairing_enabled`` setting.
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
import tempfile
import threading
import time
from pathlib import Path

# A pairing code lives this long; the satellite polls well inside it.
TTL_SECONDS = 300
# At most this many undecided requests at once: a hostile LAN box cannot fill
# the admin's screen with prompts, and a forgotten request expires on its own.
MAX_PENDING = 3
# What a paired device's key may do (the key scopes in services/access.py).
KINDS = ("satellite", "ha", "display", "ha_rest")
# An approved key stays readable by the device's poll until the device first
# uses it, or this long after the poll that first returned it.
DELIVERY_SECONDS = 120

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
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # Explicit ranges rather than ip.is_private, which on Python 3.12 also
    # covers the documentation ranges (203.0.113.0/24 and friends).
    from .access import LAN_NETWORKS
    return ip.is_loopback or any(ip.version == n.version and ip in n for n in LAN_NETWORKS)


def is_local_network_request(request) -> bool:
    """Whether an unauthenticated request truly originates on the home network.

    Decided by the access rules (services/access.py), the same ones the rest
    of the app uses: a device on the home network (the private ranges, the
    networks this device is attached to, and the owner's Home networks) or a
    program on this device passes. A request that arrived through a reverse
    proxy or tunnel (whose immediate peer is the proxy's own private IP, a
    proxy on this same machine included), from a public address, or over the
    remote-access address is refused, so the pairing and Cub-firmware
    endpoints are not silently exposed to the internet when the app is
    fronted by Pangolin, Cloudflare, or the Forager tunnel (FoodAssistant-
    gbs8). Home Assistant's Ingress is not a device asking to pair.
    """
    from ..config import settings
    from .request_origin import is_internet_request
    try:
        from .access import classify_origin
        origin = classify_origin(request)
    except Exception:
        return False
    if origin not in ("lan_direct", "plain_loopback"):
        return False
    client = request.client.host if request.client else ""
    return not is_internet_request(request.headers.get("host", ""), settings.qr_public_url,
                                   settings.tunnel_enabled, client, False)


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
    Caller holds the lock. The file holds an approved key until the device
    collects it, so it is readable by this app only (0600), like the other
    credential files, and never goes into a backup (DEVICE_LOCAL_FILES)."""
    global _mtime
    sf = _state_file()
    tmp = None
    try:
        # mkstemp creates the file 0600 under a name no other worker uses.
        fd, tmp = tempfile.mkstemp(prefix=".pairing.", dir=str(sf.parent))
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps({"requests": _requests}))
        os.replace(tmp, sf)
        tmp = None
        _mtime = sf.stat().st_mtime_ns
    except OSError:
        pass  # data_dir not writable: fall back to process-local behavior
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _prune_locked(now: float) -> None:
    global _requests
    cutoff = now - TTL_SECONDS
    _requests = {k: v for k, v in _requests.items() if v.get("created", 0) >= cutoff}


def suggest_kind(hostname: str) -> str:
    """The key scope a device of this name most likely needs."""
    name = (hostname or "").lower()
    if any(w in name for w in ("home assistant", "homeassistant", "hass")):
        return "ha"
    if "cub" in name:
        return "display"
    return "satellite"


def create_request(hostname: str, client_ip: str, kind: str = "") -> dict | None:
    """Open a pairing request; returns {request_id, code} or None when the
    pending queue is full. The code is what both screens display. ``kind``
    is the scope the device asks for (blank: suggested from the hostname)."""
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
            "kind": kind if kind in KINDS else "",
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
            if not rec.get("api_key"):
                return {"status": "approved"}
            used = False
            cid = rec.get("credential_id")
            if cid:
                try:
                    from . import credentials
                    used = bool(credentials.first_use(cid))
                except Exception:
                    used = False
            delivered = rec.get("delivered_at")
            if used or (delivered is not None and now - delivered >= DELIVERY_SECONDS):
                # The device has its key: stop handing it out.
                rec.pop("api_key", None)
                _save_locked()
                return {"status": "approved"}
            if delivered is None:
                rec["delivered_at"] = now
                _save_locked()
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
             "ip": r.get("ip", ""), "expires_in": int(r.get("created", 0) + TTL_SECONDS - now),
             "kind": r.get("kind", ""),
             "suggested_kind": r.get("kind") or suggest_kind(r.get("hostname", ""))}
            for rid, r in _requests.items() if r.get("status") == "pending"
        ]


def approve(request_id: str, api_key: str, key_name: str = "",
            credential_id: str = "") -> dict | None:
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
        rec["credential_id"] = credential_id
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
