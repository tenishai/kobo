"""agents.slack_wake: how Slack wakes the agent in a channel (2026-10-06)

Nullable, no backfill: NULL reads as "mention", which is exactly what every
existing agent did before the choice existed (the 2026-08-31 tagged-only
rule). Only new agents are born "thread", and only a PUT on the Slack tab
moves an existing one.

Revision ID: b9d4e7f2a618
Revises: a5e2c8d31f07
Create Date: 2026-10-06 12:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'b9d4e7f2a618'
down_revision = 'a5e2c8d31f07'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('agents', sa.Column('slack_wake', sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column('agents', 'slack_wake')
