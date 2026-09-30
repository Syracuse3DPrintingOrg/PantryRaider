"""recipe share audit and takedown notices

Two pieces for community-recipe compliance (FoodAssistant-59px):

- community_recipes gains audit_level and audit_findings: the share-time
  audit's verdict and its JSON-encoded findings, stored with the recipe so a
  later review sees what the audit saw. Existing rows backfill to the neutral
  "ok" / "[]" (they predate the audit), via a server_default that is dropped
  afterwards to match the model, the same pattern the slug migration used.
- takedown_notices: one row per copyright takedown notice recorded against a
  community recipe, with the counter-notice fields on the same row.

Revision ID: 7b8c9d0e1f2a
Revises: 6a7b8c9d0e1f
Create Date: 2026-07-23 12:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '7b8c9d0e1f2a'
down_revision: Union[str, None] = '6a7b8c9d0e1f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('community_recipes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('audit_level', sa.String(length=10),
                                      nullable=False, server_default='ok'))
        batch_op.add_column(sa.Column('audit_findings', sa.Text(),
                                      nullable=False, server_default='[]'))

    # Drop the server defaults now that existing rows are filled: the model
    # carries Python-side defaults only.
    with op.batch_alter_table('community_recipes', schema=None) as batch_op:
        batch_op.alter_column('audit_level', server_default=None,
                              existing_type=sa.String(length=10),
                              existing_nullable=False)
        batch_op.alter_column('audit_findings', server_default=None,
                              existing_type=sa.Text(),
                              existing_nullable=False)

    op.create_table(
        'takedown_notices',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('recipe_id', sa.Integer(), nullable=False),
        sa.Column('contact', sa.String(length=300), nullable=False),
        sa.Column('details', sa.Text(), nullable=False),
        sa.Column('created_at', sa.String(length=40), nullable=False),
        sa.Column('counter_note', sa.Text(), nullable=False),
        sa.Column('restored_at', sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(['recipe_id'], ['community_recipes.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_takedown_notices_recipe_id'), 'takedown_notices',
                    ['recipe_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_takedown_notices_recipe_id'),
                  table_name='takedown_notices')
    op.drop_table('takedown_notices')
    with op.batch_alter_table('community_recipes', schema=None) as batch_op:
        batch_op.drop_column('audit_findings')
        batch_op.drop_column('audit_level')
