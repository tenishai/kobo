"""slack_relay_apps.scopes: what Slack granted a room-made app at install (kobo#484)

Revision ID: c8e2a4f6b1d9
Revises: d1e3f5a7b9c2
Create Date: 2026-11-13 21:00:00.000000

The room asks Slack for a fixed list of bot scopes, and that list grows: a
release that lets agents send files adds one. An app installed before the
addition keeps working without it, and until now the room had no way to tell
such an app from a current one, so nothing could offer its owner the one
approval that brings it up to date. The install answer names what was
granted; the room now keeps it. Nullable with no backfill: NULL is an install
from before this column, which the room treats as owing an update.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'c8e2a4f6b1d9'
down_revision = 'd1e3f5a7b9c2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('slack_relay_apps', sa.Column('scopes', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('slack_relay_apps', 'scopes')
