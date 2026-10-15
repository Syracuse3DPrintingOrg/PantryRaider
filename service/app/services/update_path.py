"""How this install updates itself, and whether the update helper is ours.

There are four kinds:

- pi: a Pi appliance; its host bridge installs updates.
- watchtower: a Compose server with the Watchtower token set.
- server_bridge: a server set up with install.sh, whose host bridge installs
  updates the same way a Pi's does.
- container: anything else (Unraid, a hand-written compose file); the tool
  that runs the container updates it.

A server only counts as server_bridge when the bridge proves it holds the
same token as the app: GET /identity answers an HMAC over a fresh nonce,
keyed by the token. The probe is sent with a plain client and no token, so
the token never goes to a listener that has not proven itself. Once a proof
passes, a latch file remembers it, so a bridge that later stops proving
itself reads as a mismatch instead of an older bridge.

Older bridges have no /identity: their token gate answers 401 before
routing (or 404 inside their first day). Such a bridge still answers the
token-exempt GET /hostname, and reads as 'legacy' until the latch exists,
so the one update that brings it up to date still works.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from pathlib import Path

import httpx

from ..config import settings
from .ttl_cache import TTLCache

log = logging.getLogger(__name__)

KIND_PI = "pi"
KIND_WATCHTOWER = "watchtower"
KIND_SERVER_BRIDGE = "server_bridge"
KIND_CONTAINER = "container"

BRIDGE_NOT_CHECKED = "not_checked"
BRIDGE_ABSENT = "absent"
BRIDGE_VERIFIED = "verified"
BRIDGE_LEGACY = "legacy"
BRIDGE_MISMATCH = "mismatch"
BRIDGE_UNREACHABLE = "unreachable"

BRIDGE_BASE = "http://127.0.0.1:9299"
LATCH_NAME = "bridge-identity-verified"
IDENTITY_LABEL = b"pantryraider-bridge-identity-v1:"

BRIDGE_DOWN_MSG = (
    "The update helper on this server is not answering, so it is not installing "
    "updates. On the server, run sudo systemctl restart foodassistant-host-bridge "
    "and try again, or update by hand with sudo foodassistant-update.")
BRIDGE_MISMATCH_MSG = (
    "Something other than the Pantry Raider update helper is answering on this "
    "server, so the update was not sent. Update by hand on the server with "
    "sudo foodassistant-update.")
CONTAINER_HINT_MSG = "On this server, run sudo foodassistant-update to update Pantry Raider."

# A verified or legacy answer is kept five minutes, anything else 30 seconds.
_good_cache = TTLCache(300.0)
_other_cache = TTLCache(30.0)


def reset_cache() -> None:
    """Forget the cached answer."""
    _good_cache.invalidate()
    _other_cache.invalidate()


def watchtower_configured() -> bool:
    return bool(os.environ.get("WATCHTOWER_HTTP_API_TOKEN"))


def latch_path() -> Path:
    return Path(settings.data_dir) / LATCH_NAME


# Kept as a name for readers that expect a constant; it follows data_dir.
def __getattr__(name):
    if name == "LATCH":
        return latch_path()
    raise AttributeError(name)


def identity_proof(token: str, nonce: str) -> str:
    """hex HMAC-SHA256(token, label + nonce), the bridge's /identity proof."""
    return hmac.new(str(token).encode("utf-8"),
                    IDENTITY_LABEL + str(nonce).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _proof_matches(proof, token: str, nonce: str) -> bool:
    # compare_digest raises on non-ASCII text, so such an answer is a plain
    # mismatch rather than an error.
    if not isinstance(proof, str) or not proof.isascii():
        return False
    return hmac.compare_digest(proof, identity_proof(token, nonce))


def _write_latch() -> None:
    p = latch_path()
    try:
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n")
        os.chmod(p, 0o600)
    except OSError:
        log.debug("could not write the bridge identity latch", exc_info=True)


def _latched() -> bool:
    try:
        return latch_path().exists()
    except OSError:
        return False


def _client(transport=None) -> httpx.AsyncClient:
    # A plain client on purpose: never bridge_client, which sends the token.
    kwargs = {"timeout": httpx.Timeout(2.0, connect=1.0)}
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.AsyncClient(**kwargs)


async def _hostname_answers(client: httpx.AsyncClient, base: str) -> bool:
    try:
        r = await client.get(f"{base}/hostname")
        if r.status_code != 200:
            return False
        body = r.json()
        return isinstance(body, dict) and isinstance(body.get("hostname"), str)
    except Exception:  # noqa: BLE001
        return False


async def probe_server_bridge(transport=None, base=None) -> str:
    """'absent', 'verified', 'legacy', 'mismatch' or 'unreachable'."""
    from . import bridge
    token = bridge.bridge_token()
    if not token:
        return BRIDGE_ABSENT
    base = (base or BRIDGE_BASE).rstrip("/")
    nonce = secrets.token_hex(16)
    try:
        async with _client(transport) as client:
            try:
                r = await client.get(f"{base}/identity", params={"nonce": nonce})
            except (httpx.TransportError, httpx.InvalidURL):
                return BRIDGE_UNREACHABLE
            if r.status_code == 200:
                try:
                    body = r.json()
                except ValueError:
                    return BRIDGE_MISMATCH
                proof = body.get("proof") if isinstance(body, dict) else None
                if _proof_matches(proof, token, nonce):
                    _write_latch()
                    return BRIDGE_VERIFIED
                # The token may have been replaced on disk since it was cached
                # (a reinstall or a restore); check once against the file.
                fresh = bridge.read_bridge_token_fresh()
                if fresh and fresh != token and _proof_matches(proof, fresh, nonce):
                    _write_latch()
                    return BRIDGE_VERIFIED
                return BRIDGE_MISMATCH
            if r.status_code in (401, 404):
                if _latched():
                    return BRIDGE_MISMATCH
                if await _hostname_answers(client, base):
                    return BRIDGE_LEGACY
                return BRIDGE_UNREACHABLE
            if r.status_code == 503:
                return BRIDGE_UNREACHABLE
            return BRIDGE_MISMATCH
    except Exception:  # noqa: BLE001
        log.debug("bridge identity probe failed", exc_info=True)
        return BRIDGE_UNREACHABLE


def _result(kind: str, bridge_state: str) -> dict:
    return {"kind": kind, "bridge": bridge_state,
            "bridge_hint": bool(os.environ.get("FOODASSISTANT_FORCE_MODEL"))}


async def resolve(force: bool = False) -> dict:
    """{'kind', 'bridge', 'bridge_hint'} for this install."""
    if settings.is_pi_appliance():
        return _result(KIND_PI, BRIDGE_NOT_CHECKED)
    if watchtower_configured():
        return _result(KIND_WATCHTOWER, BRIDGE_NOT_CHECKED)
    if not force:
        cached = _good_cache.get() or _other_cache.get()
        if cached is not None:
            return dict(cached)
    state = await probe_server_bridge()
    if state in (BRIDGE_VERIFIED, BRIDGE_LEGACY, BRIDGE_MISMATCH):
        out = _result(KIND_SERVER_BRIDGE, state)
    elif state == BRIDGE_UNREACHABLE and (
            _latched() or os.environ.get("FOODASSISTANT_FORCE_MODEL")):
        out = _result(KIND_SERVER_BRIDGE, state)
    else:
        out = _result(KIND_CONTAINER, BRIDGE_ABSENT)
    reset_cache()
    if out["bridge"] in (BRIDGE_VERIFIED, BRIDGE_LEGACY):
        _good_cache.set(dict(out))
    else:
        _other_cache.set(dict(out))
    return out


def usable(r: dict) -> bool:
    r = r or {}
    return r.get("kind") == KIND_PI or (
        r.get("kind") == KIND_SERVER_BRIDGE
        and r.get("bridge") in (BRIDGE_VERIFIED, BRIDGE_LEGACY))


async def auto_update_eligible() -> bool:
    return usable(await resolve(force=True))
