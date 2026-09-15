"""Local two-factor authentication for the app's own login (FoodAssistant-x1ty).

This is the device's own authenticator-app 2FA for the LOCAL password, the
counterpart to the cloud account's 2FA. It is deliberately self-contained and
pure stdlib (hmac, hashlib, secrets), the same RFC 6238 implementation the
cloud uses, so it unit-tests against the published vectors with no clock and
no network. The QR image is drawn with the app's bundled ``qrcode`` dependency.

Recovery codes are printable one-time backups for a lost authenticator; only a
salted hash of each is stored (via ``passwords.hash_secret``), so a leaked
settings.json never yields a usable code. Everything here is a pure helper; the
settings wiring and endpoints live in config.py and routers/setup.py.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time as _time
from urllib.parse import quote, urlencode

from .passwords import hash_secret, verify_secret

# Standard authenticator-app TOTP: a 20-byte base32 secret, a 30-second step,
# 6 digits, HMAC-SHA1 (the defaults every app assumes when the otpauth URI
# omits them).
_TOTP_STEP = 30
_TOTP_DIGITS = 6


def generate_totp_secret() -> str:
    """A fresh base32 authenticator secret (160 bits, the RFC-recommended
    length for HMAC-SHA1)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


def _b32_key(secret: str) -> bytes:
    """Decode a base32 secret leniently: authenticator apps and manual entry
    drop the padding and spacing, so normalise before decoding."""
    cleaned = secret.strip().replace(" ", "").upper()
    pad = (-len(cleaned)) % 8
    return base64.b32decode(cleaned + "=" * pad)


def _hotp(secret: str, counter: int) -> str:
    """The HOTP value (RFC 4226) for one counter, as a zero-padded 6-digit
    string. TOTP is HOTP with the counter derived from the clock."""
    digest = hmac.new(_b32_key(secret), struct.pack(">Q", counter),
                      hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{binary % (10 ** _TOTP_DIGITS):0{_TOTP_DIGITS}d}"


def totp_now(secret: str, for_time: float | None = None) -> str:
    """The current 6-digit code for a secret. ``for_time`` (epoch seconds) is
    injectable so tests can pin the RFC vectors."""
    now = _time.time() if for_time is None else for_time
    return _hotp(secret, int(now) // _TOTP_STEP)


def totp_verify(secret: str, code: str, now: float | None = None,
                window: int = 1) -> bool:
    """Whether ``code`` is valid for ``secret`` right now, allowing +/- one
    30-second step by default so a slightly wrong device clock still works.
    Constant-time compare, and it rejects anything that is not six digits
    before doing any HMAC work."""
    code = (code or "").strip()
    if len(code) != _TOTP_DIGITS or not code.isdigit():
        return False
    if not secret:
        return False
    try:
        counter = int(_time.time() if now is None else now) // _TOTP_STEP
    except (TypeError, ValueError):
        return False
    for step in range(-window, window + 1):
        try:
            candidate = _hotp(secret, counter + step)
        except (ValueError, TypeError):
            return False
        if hmac.compare_digest(candidate, code):
            return True
    return False


def otpauth_uri(secret: str, account_label: str, issuer: str = "Pantry Raider") -> str:
    """The otpauth://totp/... URI an authenticator app reads from the QR code
    (or manual "add by URI"). Carries the issuer and account label plus the
    algorithm defaults, so any standard app enrolls without extra taps."""
    label = quote(f"{issuer}:{account_label}", safe=":")
    params = urlencode({
        "secret": secret,
        "issuer": issuer,
        "algorithm": "SHA1",
        "digits": _TOTP_DIGITS,
        "period": _TOTP_STEP,
    })
    return f"otpauth://totp/{label}?{params}"


def qr_data_uri(uri: str) -> str:
    """A data: URI PNG of the otpauth QR, or '' when qrcode is unavailable.

    The app already depends on qrcode[pil]; a missing library (or a render
    error) degrades gracefully to no image, and the manual secret is always
    shown alongside, so enrollment still works by typing the key."""
    try:
        import io
        import qrcode
        img = qrcode.make(uri)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


# Recovery codes: printable one-time backups for when the authenticator app is
# gone. The alphabet skips lookalikes (0/O, 1/I/L) so a person can read them off
# a screen, and only a salted hash of each is stored (single-use).
_RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def generate_recovery_codes(n: int = 10) -> list[str]:
    """``n`` fresh recovery codes in a readable XXXX-XXXX shape. The plaintext
    is shown to the owner once; only a hash of each is stored."""
    codes = []
    for _ in range(n):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(8))
        codes.append(f"{raw[:4]}-{raw[4:]}")
    return codes


def normalize_recovery_code(code: str) -> str:
    """Canonical form for hashing and comparison: uppercase, letters and digits
    only, so a person may type it with or without the dash or spaces."""
    return "".join(c for c in (code or "").upper() if c.isalnum())


def hash_recovery_codes(codes: list[str]) -> list[str]:
    """Salted hashes of a set of recovery codes, ready to store. Each is hashed
    in its normalised form so verification is forgiving of dashes and case."""
    return [hash_secret(normalize_recovery_code(c)) for c in codes]


def consume_recovery_code(code: str, hashed: list[str]) -> tuple[bool, list[str]]:
    """Try ``code`` against the stored hashes. On a match, return (True, the
    list with that one hash removed) so the caller persists the burn; on no
    match, (False, the unchanged list). Single-use: a code never works twice."""
    normalized = normalize_recovery_code(code)
    if not normalized:
        return False, list(hashed or [])
    remaining = []
    matched = False
    for h in (hashed or []):
        if not matched and verify_secret(normalized, h):
            matched = True
            continue  # burn this one
        remaining.append(h)
    return matched, remaining
