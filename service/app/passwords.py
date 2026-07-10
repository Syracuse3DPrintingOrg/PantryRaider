"""Hash the web-UI password and kiosk PIN at rest (FoodAssistant-ufwz).

The login password and kiosk PIN were stored in settings.json in plaintext and
compared directly. We now store a salted scrypt hash and verify against it, so a
leaked settings.json (or backup) does not expose the actual secret.

Only user-chosen secrets that the server can verify by re-hashing are covered
here (the password and the kiosk PIN). API keys and the TOTP secret are bearer
secrets that must be presented verbatim, so they cannot be one-way hashed and
stay as-is.

The format is self-describing so verification and migration need no extra state:

    scrypt$<n>$<r>$<p>$<salt_hex>$<hash_hex>

`looks_hashed` lets callers tell a stored hash from a legacy plaintext value, so
existing installs keep working (a legacy value is compared in constant time and
is upgraded to a hash the next time it is saved).
"""
from __future__ import annotations

import hashlib
import secrets as _secrets

# scrypt cost parameters. n must be a power of two; these are a sensible
# interactive-login default and keep verification well under a few milliseconds.
_N = 2 ** 14
_R = 8
_P = 1
_PREFIX = "scrypt$"


def looks_hashed(value: str) -> bool:
    """True when value is one of our stored hashes (not a legacy plaintext)."""
    return isinstance(value, str) and value.startswith(_PREFIX)


def hash_secret(plain: str) -> str:
    """Return a salted scrypt hash string for a plaintext secret. An empty input
    returns '' so an unset password stays unset (not a hash of the empty string)."""
    if not plain:
        return ""
    salt = _secrets.token_bytes(16)
    dk = hashlib.scrypt(plain.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"{_PREFIX}{_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def verify_secret(plain: str, stored: str) -> bool:
    """Constant-time check of a plaintext against a stored value.

    Handles both our hash format and a legacy plaintext value (so installs that
    predate hashing keep working). Returns False for empty inputs.
    """
    if not plain or not stored:
        return False
    if not looks_hashed(stored):
        # Legacy plaintext on disk: compare directly, still in constant time.
        return _secrets.compare_digest(plain, stored)
    try:
        _, n_s, r_s, p_s, salt_hex, hash_hex = stored.split("$")
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False
    dk = hashlib.scrypt(plain.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected))
    return _secrets.compare_digest(dk, expected)
