"""agents.archetype: the create-time recipe marker (team-guide spec 2026-09-14)

Nullable on purpose: None means an ordinary agent, which is every
pre-existing row -- archetypes are a create-time lane, never a backfill.
The composer reads this column to suppress the action-tool blocks org
config would otherwise hand every bundle, so a team-guide kiosk's
"never act" is a property of the composed bundle.

Revision ID: c7d3a9f21b84
Revises: b4e8f1a3c5d7
Create Date: 2026-09-14 10:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'c7d3a9f21b84'
down_revision = 'b4e8f1a3c5d7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('agents', sa.Column('archetype', sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column('agents', 'archetype')
