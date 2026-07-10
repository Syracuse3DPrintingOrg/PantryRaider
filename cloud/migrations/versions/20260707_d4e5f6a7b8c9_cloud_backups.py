"""cloud backups

Adds the cloud_backups table behind the Premium cloud-backup feature: one row
per stored kitchen backup (account, uploading instance, on-disk filename, size,
created_at). The zip itself lives on the VPS filesystem, not in the database,
so this table stays small. Purely additive: one new table, nothing touched on
existing data.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-07 13:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'cloud_backups',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('instance_id', sa.Integer(), nullable=True),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id']),
        sa.ForeignKeyConstraint(['instance_id'], ['instances.id'],
                                ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('cloud_backups', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_cloud_backups_account_id'),
            ['account_id'], unique=False)
        batch_op.create_index(
            batch_op.f('ix_cloud_backups_instance_id'),
            ['instance_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('cloud_backups', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cloud_backups_instance_id'))
        batch_op.drop_index(batch_op.f('ix_cloud_backups_account_id'))
    op.drop_table('cloud_backups')
