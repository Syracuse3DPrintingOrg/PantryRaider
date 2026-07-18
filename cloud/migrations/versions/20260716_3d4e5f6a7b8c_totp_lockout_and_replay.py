"""account second-factor lockout and replay guard

Adds three additive accounts columns that harden two-factor sign-in against
brute force and replay (FoodAssistant-c2om):

- totp_failures: consecutive wrong second-factor codes, its own counter kept
  apart from the password failed_logins so a pre-auth wrong-password flood can
  never lock a member out of their own second factor.
- totp_locked_until: an ISO timestamp until which the code prompt is refused
  once totp_failures crosses the account lockout threshold.
- totp_last_step: the newest 30-second TOTP time-step the account has accepted,
  so a captured code cannot be replayed inside its +/- one-step window.

Purely additive: each column defaults to 0 / empty, so every existing account
keeps its current standing and nothing about existing data changes.

Revision ID: 3d4e5f6a7b8c
Revises: 2c3d4e5f6a7b
Create Date: 2026-07-16 12:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3d4e5f6a7b8c'
down_revision: Union[str, None] = '2c3d4e5f6a7b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add each NOT NULL column with a server_default so existing account rows
    # backfill in one statement, then drop the defaults so the final schema
    # matches the model (which carries only Python-side defaults), keeping
    # `alembic check` clean.
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('totp_failures', sa.Integer(),
                                      nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('totp_locked_until', sa.String(length=40),
                                      nullable=False, server_default=''))
        batch_op.add_column(sa.Column('totp_last_step', sa.Integer(),
                                      nullable=False, server_default='0'))
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.alter_column('totp_failures', server_default=None,
                              existing_type=sa.Integer(), existing_nullable=False)
        batch_op.alter_column('totp_locked_until', server_default=None,
                              existing_type=sa.String(length=40),
                              existing_nullable=False)
        batch_op.alter_column('totp_last_step', server_default=None,
                              existing_type=sa.Integer(), existing_nullable=False)


def downgrade() -> None:
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.drop_column('totp_last_step')
        batch_op.drop_column('totp_locked_until')
        batch_op.drop_column('totp_failures')
