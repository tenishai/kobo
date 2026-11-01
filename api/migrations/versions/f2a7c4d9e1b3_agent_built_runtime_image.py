"""agents.built_runtime_image: the runtime an agent is built on right now (2026-11-01)

Revision ID: f2a7c4d9e1b3
Revises: b4d8e2f6a1c3
Create Date: 2026-11-01 22:00:00.000000

`runtime_image` is the choice (NULL = follow the room's pin); this is the
fact: the ref the agent repository's Dockerfile `FROM` line carries, written
by every path that writes that line and backfilled at boot from the file
(`kobo_api.runtime_truth`). Before this the Harness page compared the room's
pin with itself for every agent that followed the room, and said "up to
date" while the agent still ran an older runtime. Nullable, no default:
NULL means not read yet, and the page says so.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'f2a7c4d9e1b3'
down_revision = 'b4d8e2f6a1c3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('agents', sa.Column('built_runtime_image', sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column('agents', 'built_runtime_image')
