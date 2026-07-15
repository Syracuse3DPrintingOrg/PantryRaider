"""Credential handling: password hashing, bearer tokens, Stripe signatures.

Everything here is stdlib (hashlib, hmac, secrets), deliberately avoiding a
heavier password library. scrypt's parameters are encoded into the stored
hash so they can be raised later without invalidating existing accounts.
All helpers are pure, so they unit-test without a database or network.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time as _time
from urllib.parse import quote, urlencode

# scrypt parameters: n is the CPU/memory cost (2^15 keeps login under ~100ms
# on a small VPS while staying far above fast-hash brute-force territory).
# The n=2^15, r=8 combination needs 32 MiB of state, just over OpenSSL's
# default cap, so maxmem is raised explicitly.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024


def hash_password(password: str) -> str:
    """Hash a password as 'scrypt$n$r$p$salthex$keyhex'."""
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt,
                         n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                         maxmem=_SCRYPT_MAXMEM)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Check a password against a stored hash. False on any malformed input."""
    try:
        algo, n, r, p, salt_hex, key_hex = stored.split("$")
        if algo != "scrypt":
            return False
        key = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                             n=int(n), r=int(r), p=int(p),
                             maxmem=_SCRYPT_MAXMEM)
        return hmac.compare_digest(key.hex(), key_hex)
    except (ValueError, AttributeError):
        return False


# A small blocklist of the passwords that dominate credential-stuffing
# lists. Not a substitute for a full check, but it stops the worst choices
# with no dependency. Compared case-insensitively.
_COMMON_PASSWORDS = frozenset({
    "password", "password1", "password123", "12345678", "123456789",
    "1234567890", "qwertyuiop", "qwerty123", "111111111", "letmein",
    "iloveyou", "welcome1", "admin123", "changeme", "passw0rd",
    "pantryraider", "forager1", "trustno1", "sunshine1", "football1",
})

MIN_PASSWORD_LENGTH = 10


def password_problem(password: str, email: str = "") -> str | None:
    """A user-facing reason the password is unacceptable, or None if it is
    fine. Enforced everywhere a password is set: signup and change."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Your password must be at least {MIN_PASSWORD_LENGTH} characters."
    low = password.strip().lower()
    if low in _COMMON_PASSWORDS:
        return "That password is too common. Please choose a less guessable one."
    if email and low == email.strip().lower():
        return "Your password cannot be the same as your email address."
    return None


# A best-effort, in-code set of throwaway-email domains. It is deliberately
# small and hand-curated (no external dependency, no live blocklist feed), so
# it will miss new services and the occasional alias; the goal is only to turn
# away the obvious burner addresses that never receive a real person's mail.
# Matched case-insensitively against the domain after the "@".
_DISPOSABLE_EMAIL_DOMAINS = frozenset({
    "mailinator.com", "guerrillamail.com", "guerrillamail.info",
    "guerrillamail.net", "guerrillamail.org", "sharklasers.com",
    "grr.la", "10minutemail.com", "10minutemail.net", "20minutemail.com",
    "tempmail.com", "temp-mail.org", "tempmail.net", "tempr.email",
    "throwawaymail.com", "trashmail.com", "trashmail.net", "trashmail.me",
    "getnada.com", "nada.email", "yopmail.com", "yopmail.net",
    "dispostable.com", "maildrop.cc", "mailnesia.com", "mohmal.com",
    "fakeinbox.com", "spam4.me", "mytemp.email", "emailondeck.com",
    "getairmail.com", "mailcatch.com", "moakt.com", "tempinbox.com",
    "burnermail.io", "discard.email", "einrot.com",
})


def email_is_disposable(email: str) -> bool:
    """Whether the address uses a known throwaway / temporary-mail domain.

    Best-effort only (see the curated set above). Compares the domain after
    the "@" case-insensitively; returns False for anything without a domain."""
    _, _, domain = email.strip().lower().partition("@")
    return bool(domain) and domain in _DISPOSABLE_EMAIL_DOMAINS


def hash_ip(ip: str, pepper: str) -> str:
    """A short, one-way stand-in for a client address: the first 16 hex
    characters of sha256(pepper + ip).

    Used wherever an anonymous visitor needs a stable per-address identity
    (report dedupe) without the database ever holding the raw address. The
    server-side pepper keeps the short hash from being brute-forced back to
    an IP from a database dump alone. Pure and deterministic, so the same
    address always dedupes to the same key."""
    return hashlib.sha256((pepper + ip).encode()).hexdigest()[:16]


def new_token(prefix: str) -> str:
    """A fresh bearer token: 'prs_' for portal sessions, 'prc_' for instances.

    256 bits of randomness; the prefix makes a leaked token identifiable in
    logs and scanners without weakening it."""
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def token_hash(token: str) -> str:
    """The stored form of a token. Plain SHA-256 is fine here (unlike
    passwords, tokens are high-entropy, so there is nothing to brute-force)."""
    return hashlib.sha256(token.encode()).hexdigest()


def new_pairing_code() -> str:
    """A short code a person can read off the portal and type into the app.

    The alphabet skips lookalikes (0/O, 1/I/L). 8 characters over ~28 symbols
    is ~48 bits, plenty for a single-use credential that expires in minutes."""
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


def verify_stripe_signature(payload: bytes, header: str, secret: str,
                            now: int, tolerance: int = 300) -> bool:
    """Verify a Stripe-Signature header against the raw request body.

    Stripe signs 'timestamp.payload' with HMAC-SHA256 using the endpoint
    secret and sends 't=<ts>,v1=<sig>[,v1=...]'. Any matching v1 passes.
    ``now`` is injected so the timestamp tolerance is unit-testable."""
    try:
        parts = dict(
            item.split("=", 1) for item in header.split(",") if "=" in item
        )
        ts = int(parts.get("t", ""))
    except (ValueError, AttributeError):
        return False
    if abs(now - ts) > tolerance:
        return False
    signed = f"{ts}.".encode() + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    sigs = [item.split("=", 1)[1] for item in header.split(",")
            if item.startswith("v1=")]
    return any(hmac.compare_digest(expected, s) for s in sigs)


# --- Time-based one-time passwords (RFC 6238) for two-factor sign-in ---
#
# Standard authenticator-app TOTP: a 20-byte base32 secret, a 30-second step,
# 6 digits, HMAC-SHA1 (the defaults every app assumes when the otpauth URI
# omits them). All pure stdlib, and time is injectable so the whole thing
# unit-tests against the RFC's published vectors without a clock.

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


def otpauth_uri(secret: str, account_email: str, issuer: str = "Forager") -> str:
    """The otpauth://totp/... URI an authenticator app reads from the QR code
    (or manual "add by URI"). Carries the issuer and account label plus the
    algorithm defaults, so any standard app enrolls without extra taps."""
    # Keep the issuer:account colon literal (the conventional otpauth label
    # shape); the "@" in the email still percent-encodes.
    label = quote(f"{issuer}:{account_email}", safe=":")
    params = urlencode({
        "secret": secret,
        "issuer": issuer,
        "algorithm": "SHA1",
        "digits": _TOTP_DIGITS,
        "period": _TOTP_STEP,
    })
    return f"otpauth://totp/{label}?{params}"


# Recovery codes: printable one-time backups for when the authenticator app
# is gone. The alphabet skips lookalikes (0/O, 1/I/L) so a person can read
# them off a screen, and they are stored only as hashes (single-use).
_RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def generate_recovery_codes(n: int = 10) -> list[str]:
    """``n`` fresh recovery codes in a readable XXXX-XXXX shape. The plaintext
    is shown to the owner once; only ``token_hash`` of each is stored."""
    codes = []
    for _ in range(n):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(8))
        codes.append(f"{raw[:4]}-{raw[4:]}")
    return codes


def normalize_recovery_code(code: str) -> str:
    """Canonical form for hashing and comparison: uppercase, letters and
    digits only, so a person may type it with or without the dash or spaces."""
    return "".join(c for c in (code or "").upper() if c.isalnum())
