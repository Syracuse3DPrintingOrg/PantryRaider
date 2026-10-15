"""unique tunnel peer key and address

A WireGuard public key names exactly one peer on the VPS, and a tunnel IP
routes to exactly one kitchen. Enable already allocates both under a lock;
these unique indexes make the database refuse a duplicate too. The tunnel_ip
index existed but was not unique, so it is rebuilt as a unique one.

Revision ID: d58e2b7a91c3
Revises: a7c3e91f4b20
Create Date: 2026-10-15 18:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd58e2b7a91c3'
down_revision: Union[str, None] = 'a7c3e91f4b20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('tunnel_peers', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tunnel_peers_tunnel_ip'))
        batch_op.create_index(batch_op.f('ix_tunnel_peers_tunnel_ip'),
                              ['tunnel_ip'], unique=True)
        batch_op.create_index(batch_op.f('ix_tunnel_peers_public_key'),
                              ['public_key'], unique=True)


def downgrade() -> None:
    with op.batch_alter_table('tunnel_peers', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tunnel_peers_public_key'))
        batch_op.drop_index(batch_op.f('ix_tunnel_peers_tunnel_ip'))
        batch_op.create_index(batch_op.f('ix_tunnel_peers_tunnel_ip'),
                              ['tunnel_ip'], unique=False)
