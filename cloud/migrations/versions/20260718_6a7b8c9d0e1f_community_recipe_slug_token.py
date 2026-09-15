"""community recipe slug and share token

Gives every community recipe a readable, non-enumerable canonical URL
(<slug>-<token>) in place of its sequential id (peg2). Two columns on
community_recipes:

- slug: a cosmetic, title-derived slug (may repeat or change).
- share_token: an unguessable base36 token, unique, the actual resolver key.

Existing rows are backfilled in this same migration: every pre-existing recipe
gets a slug from its title and a fresh unique token, so its old ?id= link keeps
resolving (the app now redirects it to the canonical URL) and nothing that was
already shared breaks. The backfill guards on ``share_token IS NULL`` so a
retried run never overwrites a token it already assigned.

SQLite-batch clean: the columns are added first (slug with a server_default so
existing rows fill in, dropped afterwards to match the model), then the token
is backfilled, then the column is made NOT NULL and its unique index created.

Revision ID: 6a7b8c9d0e1f
Revises: 5f6a7b8c9d0e
Create Date: 2026-07-18 13:00:00.000000+00:00
"""
from __future__ import annotations

import re
import secrets
import string
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6a7b8c9d0e1f'
down_revision: Union[str, None] = '5f6a7b8c9d0e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept in step with app/routers/recipes.py (slugify / new_share_token). Defined
# locally so the migration never depends on app code that may change later.
_TOKEN_ALPHABET = string.ascii_lowercase + string.digits


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:180]
    return slug or "recipe"


def _new_token(taken: set[str]) -> str:
    while True:
        token = "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(10))
        if token not in taken:
            taken.add(token)
            return token


def upgrade() -> None:
    # Add the columns. slug gets a server_default so existing rows fill in with
    # one statement; share_token starts nullable so the backfill can assign a
    # unique value per row before the column is locked down.
    with op.batch_alter_table('community_recipes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('slug', sa.String(length=200),
                                      nullable=False, server_default=''))
        batch_op.add_column(sa.Column('share_token', sa.String(length=32),
                                      nullable=True))

    conn = op.get_bind()
    # Any tokens somehow already present (a retried run) are off-limits for new
    # ones, so the unique index below can never be tripped by the backfill.
    taken = {
        row[0] for row in conn.execute(sa.text(
            "SELECT share_token FROM community_recipes "
            "WHERE share_token IS NOT NULL AND share_token != ''")).fetchall()
    }
    rows = conn.execute(sa.text(
        "SELECT id, title FROM community_recipes "
        "WHERE share_token IS NULL OR share_token = ''")).fetchall()
    for row_id, title in rows:
        conn.execute(
            sa.text("UPDATE community_recipes SET slug = :slug, "
                    "share_token = :token WHERE id = :id"),
            {"slug": _slugify(str(title or "")), "token": _new_token(taken),
             "id": row_id})

    # Lock it down: drop slug's server_default (the model carries only a
    # Python-side default), make the token NOT NULL, and add the unique index
    # the by-token resolver reads.
    with op.batch_alter_table('community_recipes', schema=None) as batch_op:
        batch_op.alter_column('slug', server_default=None,
                              existing_type=sa.String(length=200),
                              existing_nullable=False)
        batch_op.alter_column('share_token',
                              existing_type=sa.String(length=32),
                              nullable=False)
        batch_op.create_index(batch_op.f('ix_community_recipes_share_token'),
                              ['share_token'], unique=True)


def downgrade() -> None:
    with op.batch_alter_table('community_recipes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_community_recipes_share_token'))
        batch_op.drop_column('share_token')
        batch_op.drop_column('slug')
