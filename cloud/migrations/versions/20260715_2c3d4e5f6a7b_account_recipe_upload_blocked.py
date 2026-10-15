"""account recipe_upload_blocked

Adds the additive accounts.recipe_upload_blocked flag, the admin's explicit
after-abuse switch for recipe uploads. Uploads are automatic for any account
with a linked kitchen that has checked in, so revoking by hand needs a flag
that wins over that automatic rule; unchecking the old allow flag alone
cannot. Purely additive: the column defaults to 0, so every existing account
keeps its current standing and nothing about existing data changes.

Revision ID: 2c3d4e5f6a7b
Revises: 1b2c3d4e5f6a
Create Date: 2026-07-15 12:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2c3d4e5f6a7b'
down_revision: Union[str, None] = '1b2c3d4e5f6a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add the NOT NULL column with a server_default so existing account rows
    # backfill to 0 in one statement, then drop the default so the final schema
    # matches the model (which carries only a Python-side default), keeping
    # `alembic check` clean.
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('recipe_upload_blocked', sa.Integer(),
                                      nullable=False, server_default='0'))
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.alter_column('recipe_upload_blocked', server_default=None,
                              existing_type=sa.Integer(), existing_nullable=False)


def downgrade() -> None:
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.drop_column('recipe_upload_blocked')
