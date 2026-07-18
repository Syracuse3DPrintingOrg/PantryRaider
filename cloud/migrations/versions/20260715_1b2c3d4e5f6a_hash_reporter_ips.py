"""hash anonymous reporter addresses

shared_recipe_reports.reporter_key used to hold the raw client address for
an anonymous reporter ("ip:<address>"). New reports store a short peppered
hash instead (security.hash_ip: the first 16 hex characters of
sha256(pepper + address), pepper from CLOUD_REPORT_IP_PEPPER); this rewrites
every existing raw row through the same hash so no client address remains
in the database. Dedupe is preserved: the same address still maps to the
same key. Account-based keys ("acct:<id>") are untouched. One-way on
purpose; downgrade cannot restore the addresses and does not try.

Revision ID: 1b2c3d4e5f6a
Revises: 0a1b2c3d4e5f
Create Date: 2026-07-15 12:05:00.000000+00:00
"""
from __future__ import annotations

import hashlib
import os
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1b2c3d4e5f6a'
down_revision: Union[str, None] = '0a1b2c3d4e5f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _hash_ip(ip: str, pepper: str) -> str:
    # Must match app.security.hash_ip exactly, so rows rewritten here dedupe
    # against keys the running app writes from now on.
    return hashlib.sha256((pepper + ip).encode()).hexdigest()[:16]


def upgrade() -> None:
    # The same env var the app's settings read (CLOUD_ prefix), so the
    # migration and the runtime hash with the same pepper.
    pepper = os.environ.get("CLOUD_REPORT_IP_PEPPER", "")
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, reporter_key FROM shared_recipe_reports "
        "WHERE reporter_key LIKE 'ip:%'")).fetchall()
    for row_id, key in rows:
        raw = str(key)[3:]
        conn.execute(
            sa.text("UPDATE shared_recipe_reports SET reporter_key = :key "
                    "WHERE id = :id"),
            {"key": f"ip:{_hash_ip(raw, pepper)}", "id": row_id})


def downgrade() -> None:
    # The hash is one-way; the raw addresses are gone for good, which is the
    # point. Nothing to undo.
    pass
