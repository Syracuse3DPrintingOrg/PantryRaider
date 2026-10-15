"""subscription plan and unattributed subscriptions

Stripe does not order its webhook deliveries (FoodAssistant-fa7), so the
webhook now stores what each event says on the subscription row and computes
the entitlement from the stored rows:

- subscriptions gains plan, the plan its price maps to. Existing rows
  backfill from the account's current Stripe entitlement, which is the best
  record of what they bought.
- subscriptions.account_id becomes nullable, so a subscription event that
  arrives before its checkout event can be stored and attributed later.

Revision ID: 0e1f2a3b4c5d
Revises: 9d0e1f2a3b4c
Create Date: 2026-10-14 14:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0e1f2a3b4c5d'
down_revision: Union[str, None] = '9d0e1f2a3b4c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('plan', sa.String(length=40),
                                      nullable=False, server_default=''))
        batch_op.alter_column('account_id', existing_type=sa.Integer(),
                              nullable=True)
    op.execute(
        "UPDATE subscriptions SET plan = COALESCE(("
        "SELECT e.plan FROM entitlements e "
        "WHERE e.account_id = subscriptions.account_id "
        "AND e.source IN ('stripe', '') "
        "ORDER BY e.id LIMIT 1), '')")
    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.alter_column('plan', server_default=None,
                              existing_type=sa.String(length=40),
                              existing_nullable=False)


def downgrade() -> None:
    op.execute("DELETE FROM subscriptions WHERE account_id IS NULL")
    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.alter_column('account_id', existing_type=sa.Integer(),
                              nullable=False)
        batch_op.drop_column('plan')
