"""agent_user_grants.role: how far a per-person grant reaches

Issue #309. `view` is what every grant meant before this column existed (see
the agent, open it, chat with it); `manage` lets the grantee through the
owner-or-operator gate as well. Server default `view`, so every existing row
keeps its meaning and no backfill is needed.

Revision ID: a3b9c7d1e2f4
Revises: d7e8f9a0b1c2
Create Date: 2026-10-29 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'a3b9c7d1e2f4'
down_revision = 'd7e8f9a0b1c2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'agent_user_grants',
        sa.Column('role', sa.String(length=16), nullable=False, server_default='view'),
    )


def downgrade() -> None:
    op.drop_column('agent_user_grants', 'role')
