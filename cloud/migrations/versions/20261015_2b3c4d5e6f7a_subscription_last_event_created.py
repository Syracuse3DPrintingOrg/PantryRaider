"""subscription last event created

Stripe does not order its webhook deliveries, and a late update could roll a
subscription back: an "active" sent before a cancellation, arriving after it,
gave the plan back for free. subscriptions gains last_event_created, the
Stripe time of the newest event applied to the row, so an older event is
recognised and ignored.

Adding a NOT NULL integer column with a constant default only touches the
catalog on PostgreSQL 11 and later (no table rewrite), so this is safe to run
on the live database.

Revision ID: 2b3c4d5e6f7a
Revises: 0e1f2a3b4c5d
Create Date: 2026-10-15 12:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '2b3c4d5e6f7a'
down_revision: Union[str, None] = '0e1f2a3b4c5d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('last_event_created', sa.Integer(),
                                      nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.drop_column('last_event_created')
