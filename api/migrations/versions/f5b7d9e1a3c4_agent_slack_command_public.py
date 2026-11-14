"""agents.slack_command_public: a slash command answered where the agent is not

Revision ID: f5b7d9e1a3c4
Revises: e4a6c8d0f2b3
Create Date: 2026-11-14 20:00:00.000000

An agent's Slack app can post in any public channel, so its slash command
could be answered in one it was never added to. That is now a choice its
owner makes, and this column holds it. False for every row: a command is
answered only in a conversation the agent is in.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'f5b7d9e1a3c4'
down_revision = 'e4a6c8d0f2b3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'agents',
        sa.Column('slack_command_public', sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column('agents', 'slack_command_public')
