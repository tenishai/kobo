"""agents.slack_rebuild_image / slack_rebuild_at: the Slack app lands by rebuild (2026-11-03)

Revision ID: d1e3f5a7b9c2
Revises: a7c9e1f3b5d7
Create Date: 2026-11-03 17:30:00.000000

Wiring an agent's Slack app is a commit to its repository, and the agent only
answers in Slack once that commit has built and rolled. Until now the row read
Live the moment the app was installed, minutes before the agent could hear a
mention. The room now remembers the image the agent ran when the app was wired
and when; while the deployment still runs that image (within fifteen minutes)
the row reads Rebuilding. Both nullable: NULL means no Slack wiring recorded.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'd1e3f5a7b9c2'
down_revision = 'a7c9e1f3b5d7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('agents', sa.Column('slack_rebuild_image', sa.String(length=512), nullable=True))
    op.add_column('agents', sa.Column('slack_rebuild_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('agents', 'slack_rebuild_at')
    op.drop_column('agents', 'slack_rebuild_image')
