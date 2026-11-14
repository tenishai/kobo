"""agents.slack_wake_emoji: the emoji whose reaction wakes an agent in Slack

Revision ID: e4a6c8d0f2b3
Revises: c8e2a4f6b1d9
Create Date: 2026-11-13 23:00:00.000000

An agent wakes in Slack on a mention, a thread it is in, or a whole channel
(`slack_wake`). This column is the second way in: the emoji an owner names,
whose reaction on a message wakes the agent about that message whatever the
mode. Slack's own names, comma-separated. Nullable with no backfill: NULL is
no emoji, which is what every agent did before this column.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'e4a6c8d0f2b3'
down_revision = 'c8e2a4f6b1d9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('agents', sa.Column('slack_wake_emoji', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('agents', 'slack_wake_emoji')
