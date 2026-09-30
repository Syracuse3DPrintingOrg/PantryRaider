"""account recipe_upload_authorized

Adds the additive accounts.recipe_upload_authorized flag that lets an admin
authorize an account to upload its own recipes from the portal without an
actively-used kitchen. Purely additive: the column defaults to 0, so every
existing account keeps the normal rule (upload needs a recently-seen kitchen)
and nothing about existing data changes.

Revision ID: a1b2c3d4e5f6
Revises: 1e8f8b45ec78
Create Date: 2026-07-06 21:30:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '1e8f8b45ec78'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add the NOT NULL column with a server_default so existing account rows
    # backfill to 0 in one statement, then drop the default so the final schema
    # matches the model (which carries only a Python-side default), keeping
    # `alembic check` clean.
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('recipe_upload_authorized', sa.Integer(),
                                      nullable=False, server_default='0'))
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.alter_column('recipe_upload_authorized', server_default=None,
                              existing_type=sa.Integer(), existing_nullable=False)


def downgrade() -> None:
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.drop_column('recipe_upload_authorized')
