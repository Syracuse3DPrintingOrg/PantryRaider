"""pairing-code and totp-challenge purpose

Adds a purpose column to pairing_codes and totp_challenges so a code minted
for one job can never be spent on another (FoodAssistant-cd34):

- pairing_codes.purpose: "link" redeems at /v1/pairing/redeem for a new
  instance token; "unlock" only answers /v1/instance/verify-unlock, which
  proves a Google sign-in belongs to the account an existing device is
  already linked to. Each redemption path requires its own purpose.
- totp_challenges.purpose: carried through the 2FA leg of the Google flows,
  so the provision code minted after a correct second-factor code keeps the
  purpose the flow started with.

Purely additive: both columns backfill to "link", which is exactly what every
pre-existing row was minted for, so nothing about existing data changes.

Revision ID: 4e5f6a7b8c9d
Revises: 3d4e5f6a7b8c
Create Date: 2026-07-16 12:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4e5f6a7b8c9d'
down_revision: Union[str, None] = '3d4e5f6a7b8c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add each NOT NULL column with a server_default so existing rows backfill
    # in one statement, then drop the default so the final schema matches the
    # model (which carries only a Python-side default), keeping
    # `alembic check` clean.
    with op.batch_alter_table('pairing_codes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('purpose', sa.String(length=20),
                                      nullable=False, server_default='link'))
    with op.batch_alter_table('pairing_codes', schema=None) as batch_op:
        batch_op.alter_column('purpose', server_default=None,
                              existing_type=sa.String(length=20),
                              existing_nullable=False)
    with op.batch_alter_table('totp_challenges', schema=None) as batch_op:
        batch_op.add_column(sa.Column('purpose', sa.String(length=20),
                                      nullable=False, server_default='link'))
    with op.batch_alter_table('totp_challenges', schema=None) as batch_op:
        batch_op.alter_column('purpose', server_default=None,
                              existing_type=sa.String(length=20),
                              existing_nullable=False)


def downgrade() -> None:
    with op.batch_alter_table('totp_challenges', schema=None) as batch_op:
        batch_op.drop_column('purpose')
    with op.batch_alter_table('pairing_codes', schema=None) as batch_op:
        batch_op.drop_column('purpose')
