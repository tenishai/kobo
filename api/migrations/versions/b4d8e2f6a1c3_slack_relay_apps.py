"""slack_relay_apps: the Slack apps the room makes for agents (kobo#371)

Revision ID: b4d8e2f6a1c3
Revises: e1f2a3b4c5d6
Create Date: 2026-11-01 18:00:00.000000

One row per agent whose Slack app the room created through the relay flow:
the relay id Slack posts events to (minted before the app exists, because
Slack verifies the request URL during create), the app's id and the three
credentials the relay and the install callback need without a secrets
platform round trip, the single-use install nonce, and what the install
recorded (workspace, app name). The bot token is never here; it goes
through the agent's secrets lane. Additive; nothing else changes.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'b4d8e2f6a1c3'
down_revision = 'e1f2a3b4c5d6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'slack_relay_apps',
        sa.Column('relay_id', sa.String(length=32), primary_key=True),
        sa.Column('agent_name', sa.String(length=63), nullable=False),
        sa.Column('app_id', sa.String(length=64), nullable=True),
        sa.Column('client_id', sa.String(length=64), nullable=True),
        sa.Column('client_secret', sa.Text(), nullable=True),
        sa.Column('signing_secret', sa.Text(), nullable=True),
        sa.Column('relay_secret', sa.Text(), nullable=False),
        sa.Column('oauth_state', sa.String(length=64), nullable=True),
        sa.Column('oauth_state_expires_at', sa.DateTime(), nullable=True),
        sa.Column('workspace_name', sa.String(length=200), nullable=True),
        sa.Column('workspace_id', sa.String(length=32), nullable=True),
        sa.Column('app_name', sa.String(length=200), nullable=True),
        sa.Column('wired_at', sa.DateTime(), nullable=True),
        sa.Column('installed_at', sa.DateTime(), nullable=True),
        sa.Column('created_by', sa.String(length=320), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('agent_name', name='uq_slack_relay_apps_agent_name'),
    )


def downgrade() -> None:
    op.drop_table('slack_relay_apps')
