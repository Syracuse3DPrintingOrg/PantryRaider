"""pkce code challenge on linking codes

pairing_codes and totp_challenges gain code_challenge (FoodAssistant-jyfug):
the PKCE S256 challenge a kitchen sends when it opens Google sign-in. A
linking code minted with one only redeems with the matching code_verifier.
Existing rows backfill to "" (no challenge, redeemable as before) via a
server_default that is dropped afterwards to match the model.

Revision ID: a7c3e91f4b20
Revises: 3c4d5e6f7a8b
Create Date: 2026-10-15 15:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7c3e91f4b20'
down_revision: Union[str, None] = '3c4d5e6f7a8b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ('pairing_codes', 'totp_challenges')


def upgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column('code_challenge',
                                          sa.String(length=64),
                                          nullable=False, server_default=''))
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column('code_challenge', server_default=None,
                                  existing_type=sa.String(length=64),
                                  existing_nullable=False)


def downgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column('code_challenge')
