"""agents.colleague_mode: the owner's per-agent colleague mode (issue #131)

Nullable, no backfill: NULL reads as "default", which is exactly what every
existing agent has (every colleague tool and the consult-first doctrine).
Only a PUT on the agent's colleague mode moves one.

Revision ID: e5a9c3f17b20
Revises: b9d4e7f2a618
Create Date: 2026-10-12 12:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'e5a9c3f17b20'
down_revision = 'b9d4e7f2a618'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('agents', sa.Column('colleague_mode', sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column('agents', 'colleague_mode')
