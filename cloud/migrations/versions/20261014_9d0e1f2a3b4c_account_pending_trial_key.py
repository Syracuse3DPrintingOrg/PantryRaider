"""account pending trial key

accounts gains pending_trial_key (FoodAssistant-fa2): the install key given
at signup, held until the email address is confirmed, because the free trial
now starts on verification rather than on signup. Existing rows backfill to
"" (their trial was already decided) via a server_default that is dropped
afterwards to match the model.

Revision ID: 9d0e1f2a3b4c
Revises: 1a2b3c4d5e6f
Create Date: 2026-10-14 13:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '9d0e1f2a3b4c'
down_revision: Union[str, None] = '1a2b3c4d5e6f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pending_trial_key', sa.String(length=64),
                                      nullable=False, server_default=''))
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.alter_column('pending_trial_key', server_default=None,
                              existing_type=sa.String(length=64),
                              existing_nullable=False)


def downgrade() -> None:
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.drop_column('pending_trial_key')
