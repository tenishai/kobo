"""agents.slack_bot_mentions, agents.slack_read: another app's mention, and reading channels

Revision ID: c1e3a5b7d9f2
Revises: b8d0f2a4c6e9
Create Date: 2026-11-17 22:00:00.000000

Two choices an agent's owner makes about Slack. What another Slack app's
mention of the agent may do: NULL for every row, which reads as `knowledge`,
the way every agent has answered one so far. And whether the agent may read
the channels its app has been invited to: false for every row, since no
agent could until its owner says so.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'c1e3a5b7d9f2'
down_revision = 'b8d0f2a4c6e9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('agents', sa.Column('slack_bot_mentions', sa.String(length=16), nullable=True))
    op.add_column(
        'agents',
        sa.Column('slack_read', sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column('agents', 'slack_read')
    op.drop_column('agents', 'slack_bot_mentions')
