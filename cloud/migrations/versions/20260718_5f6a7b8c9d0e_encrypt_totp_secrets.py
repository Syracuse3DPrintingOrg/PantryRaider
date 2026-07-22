"""encrypt two-factor authenticator secrets at rest

accounts.totp_secret used to hold the raw base32 authenticator seed in
plaintext. That seed is directly usable (anyone holding it can mint the
account's live six-digit codes), so a database leak defeated 2FA for every
enrolled account. This widens the column and rewrites every existing plaintext
seed as a Fernet token under CLOUD_TOTP_SECRET_KEY, the same key the running
app now encrypts and decrypts with (app/totp_crypto.py), so a dump alone no
longer yields a usable second factor (jyh0).

Two safety properties the deploy relies on:

- Fail loud, not silent: if any account has 2FA on but CLOUD_TOTP_SECRET_KEY is
  unset, the migration raises rather than leave usable seeds in the clear. Set
  the key in the VPS env file before deploying.
- Re-run tolerant: an already-encrypted value (it decrypts under the key) is
  left untouched, so running the upgrade twice never double-encrypts.

One-way for the data: downgrade narrows the column back but cannot restore a
plaintext seed from ciphertext, and a token longer than 64 chars will not fit,
so a real downgrade means clearing 2FA first. The widen itself is reversible.

Revision ID: 5f6a7b8c9d0e
Revises: 4e5f6a7b8c9d
Create Date: 2026-07-18 12:00:00.000000+00:00
"""
from __future__ import annotations

import os
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5f6a7b8c9d0e'
down_revision: Union[str, None] = '4e5f6a7b8c9d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _fernet():
    # The same env var the app's settings read (CLOUD_ prefix) and the same
    # plain Fernet the app uses, so tokens written here decrypt at runtime and
    # vice versa. Built lazily so the widen still runs on a deploy with no
    # enrolled accounts and no key set.
    from cryptography.fernet import Fernet

    key = os.environ.get("CLOUD_TOTP_SECRET_KEY", "")
    if not key:
        raise RuntimeError(
            "CLOUD_TOTP_SECRET_KEY is not set but there are accounts with "
            "two-factor sign-in enabled. Their authenticator secrets must be "
            "encrypted at rest and cannot be without the key. Set "
            "CLOUD_TOTP_SECRET_KEY (see .env.example) and re-deploy.")
    return Fernet(key.encode())


def _is_encrypted(fernet, value: str) -> bool:
    from cryptography.fernet import InvalidToken

    try:
        fernet.decrypt(value.encode())
        return True
    except InvalidToken:
        return False


def upgrade() -> None:
    # Widen first: a Fernet token of the 32-char seed is ~140 chars, past the
    # old String(64), so the ciphertext could not otherwise be stored.
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.alter_column('totp_secret',
                              existing_type=sa.String(length=64),
                              type_=sa.String(length=255),
                              existing_nullable=False)

    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, totp_secret FROM accounts "
        "WHERE totp_enabled = 1 AND totp_secret != ''")).fetchall()
    if not rows:
        # No enrolled seeds to protect: the widen is the whole change, and a
        # first deploy with no 2FA users needs no key yet.
        return

    fernet = _fernet()
    for row_id, secret in rows:
        secret = str(secret)
        if _is_encrypted(fernet, secret):
            # Already ciphertext from a prior run of this migration; leave it.
            continue
        token = fernet.encrypt(secret.encode()).decode("ascii")
        conn.execute(
            sa.text("UPDATE accounts SET totp_secret = :s WHERE id = :id"),
            {"s": token, "id": row_id})


def downgrade() -> None:
    # Reverse the widen only. The plaintext seeds are gone for good (encryption
    # is one-way here); any stored token longer than 64 chars cannot be narrowed
    # and must be cleared before a real downgrade.
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.alter_column('totp_secret',
                              existing_type=sa.String(length=255),
                              type_=sa.String(length=64),
                              existing_nullable=False)
