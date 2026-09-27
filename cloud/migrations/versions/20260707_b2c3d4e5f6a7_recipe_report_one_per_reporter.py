"""recipe report one per reporter

Adds the unique pair (recipe_id, account_id) to recipe_reports so a member can
flag a given recipe at most once. This is what stops one account from looping
reports to auto-hide any recipe: report_count now tracks distinct reporters and
the hide threshold counts separate people, not raw calls.

Tolerates an existing library: any duplicate flags (same member, same recipe)
are collapsed to the earliest row before the constraint is added, and each
recipe's report_count is recomputed as its distinct-reporter count so the stored
tally matches what now drives the auto-hide.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-07 00:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Dedupe first: keep the earliest report per (recipe, member) and drop the
    # rest, so the new unique constraint can be added even on a library that
    # already recorded repeat flags. Works on SQLite and Postgres alike.
    op.execute(
        "DELETE FROM recipe_reports WHERE id NOT IN ("
        "SELECT MIN(id) FROM recipe_reports GROUP BY recipe_id, account_id)")
    # Recompute each recipe's tally as its distinct-reporter count (one row per
    # member after the dedupe), so the stored counter matches the new rule.
    op.execute(
        "UPDATE community_recipes SET report_count = ("
        "SELECT COUNT(*) FROM recipe_reports "
        "WHERE recipe_reports.recipe_id = community_recipes.id)")
    with op.batch_alter_table('recipe_reports', schema=None) as batch_op:
        batch_op.create_unique_constraint(
            'uq_recipe_reports_reporter', ['recipe_id', 'account_id'])


def downgrade() -> None:
    with op.batch_alter_table('recipe_reports', schema=None) as batch_op:
        batch_op.drop_constraint('uq_recipe_reports_reporter', type_='unique')
