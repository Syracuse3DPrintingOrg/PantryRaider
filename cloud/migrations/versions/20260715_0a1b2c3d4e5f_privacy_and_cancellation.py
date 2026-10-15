"""privacy and cancellation groundwork

Schema changes behind self-serve account deletion and click-to-cancel:

- subscriptions.cancel_at_period_end: mirrors Stripe's flag so the account
  page can say "your plan cancels on <date>". Backfilled to 0.
- community_recipes.submitter_account_id becomes nullable: deleting an
  account keeps published recipe content but severs the link to the person.
- admin_actions.account_id and trial_claims.account_id lose their foreign
  keys to accounts (they stay plain indexed integers): both tables must
  outlive the accounts they reference. The audit trail keeps the row that
  records a deletion, and a trial claim keeps blocking a fresh trial for the
  same install after its account is gone.

No data is touched beyond the cancel_at_period_end backfill.

Revision ID: 0a1b2c3d4e5f
Revises: f6a7b8c9d0e1
Create Date: 2026-07-15 12:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0a1b2c3d4e5f'
down_revision: Union[str, None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The baseline created its foreign keys unnamed. SQLite's batch mode needs a
# deterministic name to drop one, supplied by this convention; Postgres named
# them itself at creation time (<table>_<column>_fkey).
_NAMING = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}


def _account_fk_name(table: str) -> str:
    if op.get_bind().dialect.name == "sqlite":
        return f"fk_{table}_account_id_accounts"
    return f"{table}_account_id_fkey"


def upgrade() -> None:
    # subscriptions.cancel_at_period_end: add NOT NULL with a server_default
    # so existing rows backfill to 0, then drop the default so the final
    # schema matches the model (Python-side default only), keeping
    # `alembic check` clean.
    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cancel_at_period_end', sa.Integer(),
                                      nullable=False, server_default='0'))
    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.alter_column('cancel_at_period_end', server_default=None,
                              existing_type=sa.Integer(),
                              existing_nullable=False)

    with op.batch_alter_table('community_recipes', schema=None) as batch_op:
        batch_op.alter_column('submitter_account_id',
                              existing_type=sa.Integer(), nullable=True)

    for table in ('admin_actions', 'trial_claims'):
        with op.batch_alter_table(table, schema=None,
                                  naming_convention=_NAMING) as batch_op:
            batch_op.drop_constraint(_account_fk_name(table),
                                     type_='foreignkey')


def downgrade() -> None:
    for table in ('trial_claims', 'admin_actions'):
        with op.batch_alter_table(table, schema=None,
                                  naming_convention=_NAMING) as batch_op:
            batch_op.create_foreign_key(None, 'accounts', ['account_id'],
                                        ['id'])
    with op.batch_alter_table('community_recipes', schema=None) as batch_op:
        batch_op.alter_column('submitter_account_id',
                              existing_type=sa.Integer(), nullable=False)
    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.drop_column('cancel_at_period_end')
