"""google sub and passkey user verification

accounts gains google_sub (FoodAssistant-mla0d): Google's stable account id,
filled in on each account's next Google sign-in and checked on every one after
that. webauthn_credentials gains user_verified (FoodAssistant-9s0jr): whether
the authenticator verified the person when the passkey was added. Both columns
are nullable and existing rows stay null, so nothing changes for an account
until it next signs in.

Revision ID: 5e6f7a8b9c0d
Revises: 4d5e6f7a8b9c
Create Date: 2026-11-07 12:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '5e6f7a8b9c0d'
down_revision: Union[str, None] = '4d5e6f7a8b9c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('google_sub', sa.String(length=255),
                                      nullable=True))
        batch_op.create_index(batch_op.f('ix_accounts_google_sub'),
                              ['google_sub'], unique=True)
    with op.batch_alter_table('webauthn_credentials', schema=None) as batch_op:
        batch_op.add_column(sa.Column('user_verified', sa.Integer(),
                                      nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('webauthn_credentials', schema=None) as batch_op:
        batch_op.drop_column('user_verified')
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_accounts_google_sub'))
        batch_op.drop_column('google_sub')
