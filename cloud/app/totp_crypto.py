"""Encryption at rest for the two-factor authenticator secret.

The base32 TOTP seed is the one credential Forager stores that is directly
usable: with it, anyone can mint the account's live six-digit codes. Passwords,
recovery codes, and bearer tokens are all kept non-recoverably (hashed), so a
database leak never hands over a working credential; the TOTP seed used to be
the exception, written in plaintext. This wraps it in Fernet (AES-128-CBC with
an HMAC) under a key held outside the database, in CLOUD_TOTP_SECRET_KEY, so a
database dump alone no longer defeats two-factor sign-in for every enrolled
account.

Fail closed on purpose: with no key we refuse to read or write a secret rather
than quietly fall back to plaintext, and the app refuses to start when accounts
are already enrolled but the key is missing (ensure_totp_key_available). A
misconfigured deploy is loud, never silently insecure.

The Fernet primitives are pure given a key, so they unit-test without a
database; only the settings-backed wrappers touch configuration.
"""
from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken

from .config import settings

logger = logging.getLogger("forager.totp_crypto")

# A Fernet v1 token base64url-encodes a leading 0x80 version byte, so it always
# begins "gAAAAA". A legacy plaintext base32 seed is uppercase A-Z and 2-7 and
# can never start this way, which lets us tell "wrong or rotated key" (a
# token-shaped value that will not decrypt) apart from "legacy plaintext" (which
# decrypt_secret must pass through untouched).
_FERNET_PREFIX = "gAAAAA"


def _looks_like_fernet(value: str) -> bool:
    return value.startswith(_FERNET_PREFIX)


class TotpKeyError(RuntimeError):
    """The TOTP encryption key is missing or unusable, so a secret can be
    neither read nor written. Surfaced at startup and refused at the write
    path so a seed is never stored in the clear."""


_KEY_HELP = (
    "Generate one with: python -c \"from cryptography.fernet import Fernet; "
    "print(Fernet.generate_key().decode())\" and set it in the VPS env file."
)


def secret_key_configured() -> bool:
    """Whether a TOTP encryption key is set. Cheap gate for callers that want
    to branch before touching the crypto path."""
    return bool(settings.totp_secret_key)


def _fernet() -> Fernet:
    key = settings.totp_secret_key
    if not key:
        raise TotpKeyError(
            "CLOUD_TOTP_SECRET_KEY is not set. Two-factor secrets are stored "
            "encrypted and cannot be read or written without it. " + _KEY_HELP)
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise TotpKeyError(
            "CLOUD_TOTP_SECRET_KEY is not a valid Fernet key (expected 32 "
            "url-safe base64 bytes). " + _KEY_HELP) from exc


def encrypt_secret(plaintext: str) -> str:
    """A Fernet token for a base32 authenticator seed, to store at rest. Raises
    TotpKeyError when the key is unset, so a seed is never written in the
    clear."""
    return _fernet().encrypt(plaintext.encode()).decode("ascii")


def is_encrypted(value: str) -> bool:
    """Whether a stored totp_secret is already a Fernet token under the current
    key, rather than a legacy plaintext base32 seed. Used by the migration to
    stay idempotent (re-running never double-encrypts). Returns False when the
    value is empty, plaintext, or the key is unavailable."""
    if not value:
        return False
    try:
        _fernet().decrypt(value.encode())
        return True
    except (InvalidToken, TotpKeyError):
        return False


def decrypt_secret(value: str) -> str:
    """The raw base32 seed behind a stored totp_secret, for verification.

    Decrypts a Fernet token; tolerates a legacy plaintext seed by returning it
    unchanged, so a row written before encryption (or not yet migrated) still
    verifies. Empty in, empty out. Raises TotpKeyError only when the key is
    missing and the value is a real token, which the startup guard prevents in
    a running deployment."""
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode("ascii")
    except InvalidToken:
        # Not a token under our key: a legacy plaintext seed written before
        # encryption. Return it unchanged so the code still verifies; the next
        # disable/re-enable or the data migration replaces it with ciphertext.
        return value


def ensure_totp_key_available() -> None:
    """Startup fail-closed check: refuse to run when accounts already have 2FA
    on but the encryption key is unset.

    Their authenticator seeds are stored encrypted and cannot be read without
    the key, and enabling 2FA would be forced to write a seed we cannot protect.
    A brand-new deploy with no enrolled accounts starts fine, so a first install
    is never bricked by a key it does not yet need."""
    from .database import SessionLocal
    from .models import Account

    if settings.totp_secret_key:
        # Also fail loudly on a malformed key rather than at the first login.
        _fernet()
        # Rotation guard: if the key was changed, existing tokens no longer
        # decrypt and every enrolled user would be silently pushed onto their
        # recovery codes (decrypt_secret fails safe, but silently). Sample the
        # encrypted seeds at startup and warn loudly if any token-shaped value
        # will not decrypt under the current key, so a bad rotation is caught
        # before users are. A warning, not a raise: a running deploy with a
        # genuine key must not be bricked by one odd row.
        db = SessionLocal()
        try:
            secrets = [s for (s,) in db.query(Account.totp_secret)
                       .filter(Account.totp_enabled == 1,
                               Account.totp_secret != "").all()]
        finally:
            db.close()
        bad = sum(1 for s in secrets
                  if _looks_like_fernet(s) and not is_encrypted(s))
        if bad:
            logger.warning(
                "%s of %s enrolled two-factor secret(s) will not decrypt under "
                "the current CLOUD_TOTP_SECRET_KEY. If the key was rotated, "
                "restore the previous key: those users cannot use their "
                "authenticator app until it is, only their recovery codes.",
                bad, len(secrets))
        return

    db = SessionLocal()
    try:
        enrolled = (db.query(Account)
                    .filter(Account.totp_enabled == 1,
                            Account.totp_secret != "").count())
    finally:
        db.close()
    if enrolled:
        raise TotpKeyError(
            f"CLOUD_TOTP_SECRET_KEY is not set but {enrolled} account(s) have "
            "two-factor sign-in enabled. Their authenticator secrets are stored "
            "encrypted and cannot be read without the key. Set "
            "CLOUD_TOTP_SECRET_KEY (see .env.example) and restart. " + _KEY_HELP)
