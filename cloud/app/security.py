"""Credential handling: password hashing, bearer tokens, Stripe signatures.

Everything here is stdlib (hashlib, hmac, secrets), deliberately avoiding a
heavier password library. scrypt's parameters are encoded into the stored
hash so they can be raised later without invalidating existing accounts.
All helpers are pure, so they unit-test without a database or network.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

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
