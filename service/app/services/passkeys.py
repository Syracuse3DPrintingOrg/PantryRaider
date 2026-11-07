"""Passkey sign-in for this device's admin account (FoodAssistant-kc2cy).

A passkey (Face ID, Touch ID, Windows Hello, a phone, a security key with a
PIN) signs the owner in with one tap. The authenticator checks the person
(fingerprint, face or PIN) and holds a private key that never leaves it, so a
passkey is two factors on its own and cannot be phished: the browser only
uses it on the web address it was made for. That is why a passkey sign-in
needs no authenticator code afterwards, from home or from outside.

What lives here is the part that can be tested without a browser: the stored
records (``settings.local_passkeys``), the site name and origin a ceremony
runs under, the one-use challenges, and thin wrappers over py_webauthn. The
routes are in routers/ui.py (sign-in, on the public /ui/login path) and
routers/setup/passkeys.py (adding and removing, admin only).

Rules, from the S3DP skill webauthn-prf-vault-unlock:

- The relying party id is the host name the browser is on, taken from the
  Origin header the browser writes on every POST. Each stored passkey keeps
  the id it was made under and is only verified against that id, so a
  passkey added on kitchen.example.com is offered and accepted only there.
  The authenticator signs the id's hash itself, so a look-alike site cannot
  borrow it whatever headers it sends.
- WebAuthn needs a secure context and a domain name: HTTPS, or localhost.
  An IP address or plain http on the home network cannot use passkeys, and
  the page says so instead of offering a button that fails.
- User verification is asked for as "preferred" when adding (so Windows does
  not refuse a key before the page sees it) and required in the result: a
  key that did not check the person is turned away with a plain reason.
  Sign-in requires it outright.
- Each challenge is used once, expires in five minutes, and is tied to the
  browser session that asked for it.
"""
from __future__ import annotations

import ipaddress
import json
import secrets
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from webauthn import (base64url_to_bytes, generate_authentication_options,
                      generate_registration_options, options_to_json,
                      verify_authentication_response,
                      verify_registration_response)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria,
                                      AuthenticatorTransport,
                                      PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement,
                                      UserVerificationRequirement)

RP_NAME = "Pantry Raider"
CEREMONY_SECONDS = 300
MAX_CEREMONIES = 64
MAX_PASSKEYS = 20
SESSION_KEY = "passkey_ceremony"

_lock = threading.Lock()
_ceremonies: dict[str, dict] = {}


class PasskeyError(Exception):
    """A refusal with a message fit to show the person."""


# --- Where the ceremony runs -------------------------------------------------


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def site_for(origin_header: str) -> tuple[str, str] | None:
    """(rp_id, origin) for a browser Origin header, or None when passkeys
    cannot work there: no Origin, plain http on anything but localhost, or an
    IP address (WebAuthn takes only domain names)."""
    origin = (origin_header or "").strip()
    if not origin or origin.lower() == "null":
        return None
    try:
        parts = urlsplit(origin)
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError:
        return None
    if not host or _is_ip(host):
        return None
    if parts.scheme == "https":
        pass
    elif parts.scheme == "http" and (host == "localhost" or host.endswith(".localhost")):
        pass
    else:
        return None
    netloc = host if port is None else f"{host}:{port}"
    return host, f"{parts.scheme}://{netloc}"


# --- Stored records ----------------------------------------------------------


def _records(settings) -> list[dict]:
    raw = settings.local_passkeys if isinstance(settings.local_passkeys, list) else []
    return [r for r in raw if isinstance(r, dict) and r.get("id") and r.get("public_key")]


def _iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(),
                                  tz=timezone.utc).isoformat(timespec="seconds")


def for_site(settings, rp_id: str) -> list[dict]:
    """The stored passkeys made on ``rp_id``."""
    return [r for r in _records(settings) if r.get("rp_id") == rp_id]


def public_list(settings) -> list[dict]:
    """What the Security pane shows: names, where each works, when it was
    added and last used. No key material."""
    out = []
    for r in _records(settings):
        out.append({
            "id": r["id"], "name": r.get("name") or "Passkey",
            "rp_id": r.get("rp_id", ""), "created": r.get("created", ""),
            "last_used": r.get("last_used") or "",
            "synced": bool(r.get("backed_up")),
        })
    return out


def user_handle(settings) -> bytes:
    """The one user handle every passkey on this device shares, so a phone
    replaces its old passkey for this kitchen instead of keeping two."""
    for r in _records(settings):
        h = r.get("user_handle")
        if isinstance(h, str) and h:
            try:
                return base64url_to_bytes(h)
            except Exception:
                continue
    return secrets.token_bytes(16)


def _transports(values) -> list[AuthenticatorTransport] | None:
    out = []
    for t in values or []:
        try:
            out.append(AuthenticatorTransport(str(t)))
        except ValueError:
            continue
    return out or None


def _descriptor(rec: dict) -> PublicKeyCredentialDescriptor:
    return PublicKeyCredentialDescriptor(id=base64url_to_bytes(rec["id"]),
                                         transports=_transports(rec.get("transports")))


# --- One-use challenges ------------------------------------------------------


def _prune(now: float) -> None:
    for cid in [c for c, v in _ceremonies.items() if v["expires"] <= now]:
        _ceremonies.pop(cid, None)
    while len(_ceremonies) >= MAX_CEREMONIES:
        oldest = min(_ceremonies, key=lambda c: _ceremonies[c]["expires"])
        _ceremonies.pop(oldest, None)


def _stash(session, kind: str, challenge: bytes, rp_id: str, origin: str,
           extra: dict | None = None) -> None:
    now = time.time()
    cid = secrets.token_urlsafe(24)
    with _lock:
        _prune(now)
        _ceremonies[cid] = {"kind": kind, "challenge": challenge, "rp_id": rp_id,
                            "origin": origin, "expires": now + CEREMONY_SECONDS,
                            **(extra or {})}
    session[SESSION_KEY] = cid


def _take(session, kind: str) -> dict:
    """Spend the session's pending ceremony of ``kind``. Raises PasskeyError
    when there is none, it expired, or it was for something else."""
    cid = session.pop(SESSION_KEY, None)
    with _lock:
        row = _ceremonies.pop(cid, None) if isinstance(cid, str) else None
    if not row or row["kind"] != kind or row["expires"] <= time.time():
        raise PasskeyError("That took too long or was already used. Try again.")
    return row


def reset() -> None:
    """Forget every pending ceremony (tests)."""
    with _lock:
        _ceremonies.clear()


# --- Adding a passkey --------------------------------------------------------


def registration_options(settings, session, origin_header: str, name: str) -> dict:
    site = site_for(origin_header)
    if site is None:
        raise PasskeyError(NOT_HERE_MSG)
    rp_id, origin = site
    if len(_records(settings)) >= MAX_PASSKEYS:
        raise PasskeyError("This kitchen already has the most passkeys it can keep. "
                           "Remove one you no longer use first.")
    handle = user_handle(settings)
    from ..config import device_hostname
    label = device_hostname() or rp_id
    opts = generate_registration_options(
        rp_id=rp_id, rp_name=RP_NAME, user_id=handle,
        user_name=f"Admin ({label})", user_display_name=f"{RP_NAME} admin, {label}",
        exclude_credentials=[_descriptor(r) for r in for_site(settings, rp_id)],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED),
        timeout=CEREMONY_SECONDS * 1000,
    )
    _stash(session, "register", opts.challenge, rp_id, origin,
           {"handle": bytes_to_base64url(handle),
            "name": (str(name or "").strip()[:40] or "Passkey")})
    return json.loads(options_to_json(opts))


def verify_registration(credential: dict, challenge: bytes, rp_id: str, origin: str):
    """py_webauthn's check, the seam the tests stub."""
    return verify_registration_response(
        credential=credential, expected_challenge=challenge,
        expected_rp_id=rp_id, expected_origin=origin,
        require_user_verification=True)


def finish_registration(settings, session, origin_header: str, credential) -> dict:
    """Check the browser's answer and return the record to store. Raises
    PasskeyError with a message for the person on any failure."""
    row = _take(session, "register")
    site = site_for(origin_header)
    if site is None or site != (row["rp_id"], row["origin"]):
        raise PasskeyError(ADD_FAILED_MSG)
    if not isinstance(credential, dict):
        raise PasskeyError(ADD_FAILED_MSG)
    try:
        v = verify_registration(credential, row["challenge"], row["rp_id"], row["origin"])
    except Exception as exc:  # noqa: BLE001 - every failure reads the same to the person
        if "user verification" in str(exc).lower():
            raise PasskeyError(NO_UV_MSG) from None
        raise PasskeyError(ADD_FAILED_MSG) from None
    cred_id = bytes_to_base64url(v.credential_id)
    if any(r["id"] == cred_id for r in _records(settings)):
        raise PasskeyError("That passkey is already added.")
    transports = credential.get("response", {}).get("transports") if isinstance(
        credential.get("response"), dict) else None
    return {
        "id": cred_id,
        "public_key": bytes_to_base64url(v.credential_public_key),
        "sign_count": int(v.sign_count or 0),
        "rp_id": row["rp_id"],
        "user_handle": row["handle"],
        "name": row["name"],
        "aaguid": str(v.aaguid or ""),
        "backed_up": bool(getattr(v, "credential_backed_up", False)),
        "transports": [str(t) for t in (transports or [])][:6],
        "created": _iso(),
        "last_used": "",
    }


# --- Signing in --------------------------------------------------------------


def authentication_options(settings, session, origin_header: str) -> dict | None:
    """Request options for a sign-in here, or None when no passkey was made
    for this address (the page then keeps the button hidden)."""
    site = site_for(origin_header)
    if site is None:
        return None
    rp_id, origin = site
    recs = for_site(settings, rp_id)
    if not recs:
        return None
    opts = generate_authentication_options(
        rp_id=rp_id, allow_credentials=[_descriptor(r) for r in recs],
        user_verification=UserVerificationRequirement.REQUIRED,
        timeout=CEREMONY_SECONDS * 1000,
    )
    _stash(session, "login", opts.challenge, rp_id, origin)
    return json.loads(options_to_json(opts))


def verify_authentication(credential: dict, challenge: bytes, rp_id: str, origin: str,
                          public_key: bytes, sign_count: int):
    """py_webauthn's check, the seam the tests stub."""
    return verify_authentication_response(
        credential=credential, expected_challenge=challenge,
        expected_rp_id=rp_id, expected_origin=origin,
        credential_public_key=public_key, credential_current_sign_count=sign_count,
        require_user_verification=True)


def finish_authentication(settings, session, origin_header: str, credential) -> list[dict]:
    """Check a sign-in and return the updated passkey list to save (new
    signature counter and last-used time). Raises PasskeyError on failure."""
    row = _take(session, "login")
    site = site_for(origin_header)
    if site is None or site != (row["rp_id"], row["origin"]):
        raise PasskeyError(SIGN_IN_FAILED_MSG)
    if not isinstance(credential, dict) or not isinstance(credential.get("id"), str):
        raise PasskeyError(SIGN_IN_FAILED_MSG)
    records = _records(settings)
    rec = next((r for r in records
                if r["id"] == credential["id"] and r.get("rp_id") == row["rp_id"]), None)
    if rec is None:
        raise PasskeyError(SIGN_IN_FAILED_MSG)
    try:
        v = verify_authentication(credential, row["challenge"], row["rp_id"], row["origin"],
                                  base64url_to_bytes(rec["public_key"]),
                                  int(rec.get("sign_count") or 0))
    except Exception:  # noqa: BLE001
        raise PasskeyError(SIGN_IN_FAILED_MSG) from None
    out = []
    for r in records:
        if r["id"] == rec["id"]:
            r = {**r, "sign_count": int(v.new_sign_count or 0), "last_used": _iso()}
        out.append(r)
    return out


def without(settings, cred_id: str) -> list[dict] | None:
    """The stored list minus ``cred_id``, or None when there is no such key."""
    records = _records(settings)
    kept = [r for r in records if r["id"] != cred_id]
    return kept if len(kept) != len(records) else None


NOT_HERE_MSG = ("Passkeys work when this kitchen is opened over HTTPS by its web "
                "address, such as through Forager remote access or your own domain. "
                "They cannot be used over plain http or by IP address.")
ADD_FAILED_MSG = "That did not work. Try adding the passkey again."
NO_UV_MSG = ("That passkey did not check who you are. Use a phone or computer "
             "passkey, or set a PIN on your security key, and try again.")
SIGN_IN_FAILED_MSG = "That passkey did not work here. Try again or use your password."
