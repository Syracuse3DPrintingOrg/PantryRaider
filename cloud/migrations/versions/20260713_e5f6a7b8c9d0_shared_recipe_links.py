"""shared recipe links

Adds the two tables behind recipe share links: shared_recipes (one row per
share, addressed by its unguessable token, with the recipe content copied in)
and shared_recipe_reports (one row per distinct reporter, member or anonymous,
backing the auto-revoke threshold). Purely additive: two new tables, nothing
touched on existing data.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-13 12:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'shared_recipes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('token', sa.String(length=64), nullable=False),
        sa.Column('owner_account_id', sa.Integer(), nullable=False),
        sa.Column('recipient_account_id', sa.Integer(), nullable=True),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('ingredients', sa.Text(), nullable=False),
        sa.Column('steps', sa.Text(), nullable=False),
        sa.Column('image_url', sa.String(length=1024), nullable=False),
        sa.Column('attribution', sa.String(length=500), nullable=False),
        sa.Column('revoked', sa.Integer(), nullable=False),
        sa.Column('report_count', sa.Integer(), nullable=False),
        sa.Column('view_count', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.String(length=40), nullable=False),
        sa.Column('updated_at', sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(['owner_account_id'], ['accounts.id']),
        sa.ForeignKeyConstraint(['recipient_account_id'], ['accounts.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('shared_recipes', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_shared_recipes_token'), ['token'], unique=True)
        batch_op.create_index(
            batch_op.f('ix_shared_recipes_owner_account_id'),
            ['owner_account_id'], unique=False)
        batch_op.create_index(
            batch_op.f('ix_shared_recipes_recipient_account_id'),
            ['recipient_account_id'], unique=False)

    op.create_table(
        'shared_recipe_reports',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('share_id', sa.Integer(), nullable=False),
        sa.Column('reporter_key', sa.String(length=120), nullable=False),
        sa.Column('created_at', sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(['share_id'], ['shared_recipes.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('share_id', 'reporter_key',
                            name='uq_shared_recipe_reports_reporter'),
    )
    with op.batch_alter_table('shared_recipe_reports', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_shared_recipe_reports_share_id'),
            ['share_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('shared_recipe_reports', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_shared_recipe_reports_share_id'))
    op.drop_table('shared_recipe_reports')
    with op.batch_alter_table('shared_recipes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_shared_recipes_recipient_account_id'))
        batch_op.drop_index(batch_op.f('ix_shared_recipes_owner_account_id'))
        batch_op.drop_index(batch_op.f('ix_shared_recipes_token'))
    op.drop_table('shared_recipes')
