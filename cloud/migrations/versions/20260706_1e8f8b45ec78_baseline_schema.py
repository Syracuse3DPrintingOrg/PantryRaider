"""baseline schema

The initial Alembic revision. It captures the full Forager schema exactly as
Base.metadata.create_all produced it before migrations existed, so applying it
to an empty database yields the same tables, indexes, and constraints the app
created at startup. The live production database is brought under Alembic
control by stamping this revision (alembic stamp head) with no data change;
see migrations/README.md.

Revision ID: 1e8f8b45ec78
Revises:
Create Date: 2026-07-06 20:48:07.953663+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1e8f8b45ec78'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('accounts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('password_hash', sa.String(length=512), nullable=False),
    sa.Column('auth_provider', sa.String(length=20), nullable=False),
    sa.Column('email_verified', sa.Integer(), nullable=False),
    sa.Column('disabled', sa.Integer(), nullable=False),
    sa.Column('failed_logins', sa.Integer(), nullable=False),
    sa.Column('locked_until', sa.String(length=40), nullable=False),
    sa.Column('totp_secret', sa.String(length=64), nullable=False),
    sa.Column('totp_enabled', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_accounts_email'), ['email'], unique=True)

    op.create_table('stripe_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('event_id', sa.String(length=120), nullable=False),
    sa.Column('event_type', sa.String(length=80), nullable=False),
    sa.Column('processed_at', sa.String(length=40), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('stripe_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_stripe_events_event_id'), ['event_id'], unique=True)

    op.create_table('admin_actions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('admin_email', sa.String(length=255), nullable=False),
    sa.Column('action', sa.String(length=40), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('detail', sa.String(length=255), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('admin_actions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_admin_actions_account_id'), ['account_id'], unique=False)

    op.create_table('auth_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('expires_at', sa.String(length=40), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('auth_sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_auth_sessions_account_id'), ['account_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_auth_sessions_token_hash'), ['token_hash'], unique=True)

    op.create_table('community_recipes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('ingredients', sa.Text(), nullable=False),
    sa.Column('steps', sa.Text(), nullable=False),
    sa.Column('image_url', sa.String(length=1024), nullable=False),
    sa.Column('attribution', sa.String(length=500), nullable=False),
    sa.Column('submitter_account_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('rating_count', sa.Integer(), nullable=False),
    sa.Column('rating_sum', sa.Integer(), nullable=False),
    sa.Column('report_count', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('updated_at', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['submitter_account_id'], ['accounts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('community_recipes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_community_recipes_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_community_recipes_submitter_account_id'), ['submitter_account_id'], unique=False)

    op.create_table('email_tokens',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('purpose', sa.String(length=20), nullable=False),
    sa.Column('expires_at', sa.String(length=40), nullable=False),
    sa.Column('used', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('email_tokens', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_email_tokens_account_id'), ['account_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_email_tokens_purpose'), ['purpose'], unique=False)
        batch_op.create_index(batch_op.f('ix_email_tokens_token_hash'), ['token_hash'], unique=True)

    op.create_table('entitlements',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('plan', sa.String(length=40), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('monthly_token_quota', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('expires_at', sa.String(length=40), nullable=False),
    sa.Column('updated_at', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('entitlements', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_entitlements_account_id'), ['account_id'], unique=False)

    op.create_table('instances',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('app_version', sa.String(length=40), nullable=False),
    sa.Column('deployment_mode', sa.String(length=40), nullable=False),
    sa.Column('last_seen_at', sa.String(length=40), nullable=False),
    sa.Column('public_url', sa.String(length=255), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('instances', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_instances_account_id'), ['account_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_instances_token_hash'), ['token_hash'], unique=True)

    op.create_table('pairing_codes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code_hash', sa.String(length=64), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('expires_at', sa.String(length=40), nullable=False),
    sa.Column('redeemed', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('pairing_codes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_pairing_codes_account_id'), ['account_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_pairing_codes_code_hash'), ['code_hash'], unique=True)

    op.create_table('recovery_codes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('code_hash', sa.String(length=64), nullable=False),
    sa.Column('used', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('recovery_codes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_recovery_codes_account_id'), ['account_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_recovery_codes_code_hash'), ['code_hash'], unique=True)

    op.create_table('subscriptions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('stripe_customer_id', sa.String(length=120), nullable=False),
    sa.Column('stripe_subscription_id', sa.String(length=120), nullable=False),
    sa.Column('status', sa.String(length=40), nullable=False),
    sa.Column('current_period_end', sa.String(length=40), nullable=False),
    sa.Column('updated_at', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_subscriptions_account_id'), ['account_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_subscriptions_stripe_subscription_id'), ['stripe_subscription_id'], unique=True)

    op.create_table('totp_challenges',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('expires_at', sa.String(length=40), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('return_url', sa.String(length=2048), nullable=False),
    sa.Column('device_name', sa.String(length=120), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('totp_challenges', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_totp_challenges_account_id'), ['account_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_totp_challenges_token_hash'), ['token_hash'], unique=True)

    op.create_table('trial_claims',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('install_key', sa.String(length=64), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('trial_claims', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_trial_claims_account_id'), ['account_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_trial_claims_install_key'), ['install_key'], unique=True)

    op.create_table('recipe_ratings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('recipe_id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('stars', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['recipe_id'], ['community_recipes.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('recipe_id', 'account_id')
    )
    with op.batch_alter_table('recipe_ratings', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_recipe_ratings_account_id'), ['account_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_recipe_ratings_recipe_id'), ['recipe_id'], unique=False)

    op.create_table('recipe_reports',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('recipe_id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('reason', sa.String(length=500), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['recipe_id'], ['community_recipes.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('recipe_reports', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_recipe_reports_account_id'), ['account_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_recipe_reports_recipe_id'), ['recipe_id'], unique=False)

    op.create_table('tunnel_peers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('instance_id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('public_key', sa.String(length=64), nullable=False),
    sa.Column('tunnel_ip', sa.String(length=40), nullable=False),
    sa.Column('app_port', sa.Integer(), nullable=False),
    sa.Column('subdomain', sa.String(length=63), nullable=False),
    sa.Column('last_handshake', sa.String(length=40), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['instance_id'], ['instances.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('tunnel_peers', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tunnel_peers_account_id'), ['account_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_tunnel_peers_instance_id'), ['instance_id'], unique=True)
        batch_op.create_index(batch_op.f('ix_tunnel_peers_subdomain'), ['subdomain'], unique=True)
        batch_op.create_index(batch_op.f('ix_tunnel_peers_tunnel_ip'), ['tunnel_ip'], unique=False)

    op.create_table('usage_ledger',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('instance_id', sa.Integer(), nullable=True),
    sa.Column('month_key', sa.String(length=7), nullable=False),
    sa.Column('tokens', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['instance_id'], ['instances.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('usage_ledger', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_usage_ledger_account_id'), ['account_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_usage_ledger_instance_id'), ['instance_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_usage_ledger_month_key'), ['month_key'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('usage_ledger', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_usage_ledger_month_key'))
        batch_op.drop_index(batch_op.f('ix_usage_ledger_instance_id'))
        batch_op.drop_index(batch_op.f('ix_usage_ledger_account_id'))

    op.drop_table('usage_ledger')
    with op.batch_alter_table('tunnel_peers', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tunnel_peers_tunnel_ip'))
        batch_op.drop_index(batch_op.f('ix_tunnel_peers_subdomain'))
        batch_op.drop_index(batch_op.f('ix_tunnel_peers_instance_id'))
        batch_op.drop_index(batch_op.f('ix_tunnel_peers_account_id'))

    op.drop_table('tunnel_peers')
    with op.batch_alter_table('recipe_reports', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_recipe_reports_recipe_id'))
        batch_op.drop_index(batch_op.f('ix_recipe_reports_account_id'))

    op.drop_table('recipe_reports')
    with op.batch_alter_table('recipe_ratings', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_recipe_ratings_recipe_id'))
        batch_op.drop_index(batch_op.f('ix_recipe_ratings_account_id'))

    op.drop_table('recipe_ratings')
    with op.batch_alter_table('trial_claims', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_trial_claims_install_key'))
        batch_op.drop_index(batch_op.f('ix_trial_claims_account_id'))

    op.drop_table('trial_claims')
    with op.batch_alter_table('totp_challenges', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_totp_challenges_token_hash'))
        batch_op.drop_index(batch_op.f('ix_totp_challenges_account_id'))

    op.drop_table('totp_challenges')
    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_subscriptions_stripe_subscription_id'))
        batch_op.drop_index(batch_op.f('ix_subscriptions_account_id'))

    op.drop_table('subscriptions')
    with op.batch_alter_table('recovery_codes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_recovery_codes_code_hash'))
        batch_op.drop_index(batch_op.f('ix_recovery_codes_account_id'))

    op.drop_table('recovery_codes')
    with op.batch_alter_table('pairing_codes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_pairing_codes_code_hash'))
        batch_op.drop_index(batch_op.f('ix_pairing_codes_account_id'))

    op.drop_table('pairing_codes')
    with op.batch_alter_table('instances', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_instances_token_hash'))
        batch_op.drop_index(batch_op.f('ix_instances_account_id'))

    op.drop_table('instances')
    with op.batch_alter_table('entitlements', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_entitlements_account_id'))

    op.drop_table('entitlements')
    with op.batch_alter_table('email_tokens', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_email_tokens_token_hash'))
        batch_op.drop_index(batch_op.f('ix_email_tokens_purpose'))
        batch_op.drop_index(batch_op.f('ix_email_tokens_account_id'))

    op.drop_table('email_tokens')
    with op.batch_alter_table('community_recipes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_community_recipes_submitter_account_id'))
        batch_op.drop_index(batch_op.f('ix_community_recipes_status'))

    op.drop_table('community_recipes')
    with op.batch_alter_table('auth_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_auth_sessions_token_hash'))
        batch_op.drop_index(batch_op.f('ix_auth_sessions_account_id'))

    op.drop_table('auth_sessions')
    with op.batch_alter_table('admin_actions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_admin_actions_account_id'))

    op.drop_table('admin_actions')
    with op.batch_alter_table('stripe_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_stripe_events_event_id'))

    op.drop_table('stripe_events')
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_accounts_email'))

    op.drop_table('accounts')
