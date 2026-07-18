"""expiry observations

Adds the expiry_observations table behind community shelf-life learning: one
row per anonymous shelf-life correction submitted by an opted-in kitchen
(POST /api/learn/expiry). The schema is anonymous by design: no account or
instance link, and received_date is day-granular only. Purely additive: one
new table, nothing touched on existing data.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-14 12:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'expiry_observations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('barcode', sa.String(length=32), nullable=True),
        sa.Column('name_key', sa.String(length=120), nullable=False),
        sa.Column('storage', sa.String(length=10), nullable=False),
        sa.Column('shelf_life_days', sa.Integer(), nullable=False),
        sa.Column('suggested_days', sa.Integer(), nullable=True),
        sa.Column('suggestion_source', sa.String(length=12), nullable=False),
        sa.Column('received_date', sa.String(length=10), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('expiry_observations', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_expiry_observations_barcode'), ['barcode'],
            unique=False)
        batch_op.create_index(
            batch_op.f('ix_expiry_observations_name_key'), ['name_key'],
            unique=False)


def downgrade() -> None:
    with op.batch_alter_table('expiry_observations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_expiry_observations_name_key'))
        batch_op.drop_index(batch_op.f('ix_expiry_observations_barcode'))
    op.drop_table('expiry_observations')
