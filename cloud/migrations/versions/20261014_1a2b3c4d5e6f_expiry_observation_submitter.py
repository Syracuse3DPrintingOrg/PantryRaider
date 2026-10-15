"""expiry observation submitter tag

expiry_observations gains a nullable submitter column (FoodAssistant-fa1): a
day-scoped peppered hash of the sender's network, so the community shelf-life
feed counts distinct senders toward its k-threshold instead of raw rows.
Existing rows stay null and are treated as one submitter per received day.

Revision ID: 1a2b3c4d5e6f
Revises: 8c9d0e1f2a3b
Create Date: 2026-10-14 12:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '1a2b3c4d5e6f'
down_revision: Union[str, None] = '8c9d0e1f2a3b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('expiry_observations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('submitter', sa.String(length=16),
                                      nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('expiry_observations', schema=None) as batch_op:
        batch_op.drop_column('submitter')
