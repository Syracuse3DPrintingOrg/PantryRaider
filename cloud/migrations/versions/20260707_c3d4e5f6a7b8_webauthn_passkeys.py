"""webauthn passkeys

Adds the two tables behind passkey (WebAuthn / FIDO2) sign-in for the Forager
account: webauthn_credentials holds a registered passkey (account, credential
id, public key, signature counter, transports, nickname, timestamps) and
webauthn_challenges holds the short-lived challenge each ceremony stashes
server-side. Purely additive: two new tables, nothing touched on existing data,
so every account keeps its password and any two-factor sign-in unchanged.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-07 12:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'webauthn_credentials',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('credential_id', sa.String(length=512), nullable=False),
        sa.Column('public_key', sa.Text(), nullable=False),
        sa.Column('sign_count', sa.Integer(), nullable=False),
        sa.Column('transports', sa.String(length=120), nullable=False),
        sa.Column('nickname', sa.String(length=120), nullable=False),
        sa.Column('created_at', sa.String(length=40), nullable=False),
        sa.Column('last_used_at', sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'],
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('webauthn_credentials', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_webauthn_credentials_account_id'),
            ['account_id'], unique=False)
        batch_op.create_index(
            batch_op.f('ix_webauthn_credentials_credential_id'),
            ['credential_id'], unique=True)

    op.create_table(
        'webauthn_challenges',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('purpose', sa.String(length=20), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('challenge', sa.String(length=255), nullable=False),
        sa.Column('expires_at', sa.String(length=40), nullable=False),
        sa.Column('created_at', sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('webauthn_challenges', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_webauthn_challenges_token_hash'),
            ['token_hash'], unique=True)
        batch_op.create_index(
            batch_op.f('ix_webauthn_challenges_purpose'),
            ['purpose'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('webauthn_challenges', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_webauthn_challenges_purpose'))
        batch_op.drop_index(batch_op.f('ix_webauthn_challenges_token_hash'))
    op.drop_table('webauthn_challenges')
    with op.batch_alter_table('webauthn_credentials', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_webauthn_credentials_credential_id'))
        batch_op.drop_index(batch_op.f('ix_webauthn_credentials_account_id'))
    op.drop_table('webauthn_credentials')
