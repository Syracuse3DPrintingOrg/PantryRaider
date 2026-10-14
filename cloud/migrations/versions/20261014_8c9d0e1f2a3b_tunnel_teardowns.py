"""tunnel teardowns

A kitchen removed while the VPS tunnel agent was unreachable used to leave
its WireGuard peer and Caddy route live with nothing on record to clean them
up. tunnel_teardowns keeps each such peer (key, address, name) until the
periodic sweep gets the agent to remove it (FoodAssistant-ft1).

Revision ID: 8c9d0e1f2a3b
Revises: 7b8c9d0e1f2a
Create Date: 2026-10-14 12:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '8c9d0e1f2a3b'
down_revision: Union[str, None] = '7b8c9d0e1f2a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'tunnel_teardowns',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('public_key', sa.String(length=64), nullable=False),
        sa.Column('tunnel_ip', sa.String(length=40), nullable=False),
        sa.Column('subdomain', sa.String(length=63), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('last_error', sa.String(length=300), nullable=False),
        sa.Column('created_at', sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_tunnel_teardowns_public_key'), 'tunnel_teardowns',
                    ['public_key'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_tunnel_teardowns_public_key'),
                  table_name='tunnel_teardowns')
    op.drop_table('tunnel_teardowns')
