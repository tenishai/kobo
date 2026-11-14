"""slack_relay_apps.last_event_at, last_event_outcome: what became of the last hand-off

Revision ID: a7c9e1b3d5f6
Revises: f5b7d9e1a3c4
Create Date: 2026-11-14 22:00:00.000000

The relay hands each of Slack's events to the agent after Slack has its 200,
and until now only a log line said what became of it. An agent that runs its
team's own code can refuse every one (it does not take the room's messages
yet) or never be reached, and its page had nothing to say so with. The relay
now keeps the last outcome per app: when, and one word (`taken`, `refused`,
`lost`). Two columns rather than memory because the room runs more than one
replica and the page may be served by the one that did not hand it over.
Nullable with no backfill: NULL is an app nothing has been handed to since.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'a7c9e1b3d5f6'
down_revision = 'f5b7d9e1a3c4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('slack_relay_apps', sa.Column('last_event_at', sa.DateTime(), nullable=True))
    op.add_column('slack_relay_apps', sa.Column('last_event_outcome', sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column('slack_relay_apps', 'last_event_outcome')
    op.drop_column('slack_relay_apps', 'last_event_at')
