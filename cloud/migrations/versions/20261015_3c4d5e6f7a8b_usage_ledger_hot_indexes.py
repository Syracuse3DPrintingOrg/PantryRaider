"""usage ledger indexes for the AI gate

Every AI call sums the ledger twice before it is forwarded: the account's
tokens this month, and the whole service's tokens today for the daily
ceiling. The day's sum filtered on created_at, which had no index, so it read
the entire ledger on every call, and the ledger only ever grows. The month's
sum combined two single-column indexes. This adds an index on created_at and
a composite (account_id, month_key) index.

Revision ID: 3c4d5e6f7a8b
Revises: 2b3c4d5e6f7a
Create Date: 2026-10-15 12:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '3c4d5e6f7a8b'
down_revision: Union[str, None] = '2b3c4d5e6f7a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('usage_ledger', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_usage_ledger_created_at'),
                              ['created_at'], unique=False)
        batch_op.create_index('ix_usage_ledger_account_month',
                              ['account_id', 'month_key'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('usage_ledger', schema=None) as batch_op:
        batch_op.drop_index('ix_usage_ledger_account_month')
        batch_op.drop_index(batch_op.f('ix_usage_ledger_created_at'))
