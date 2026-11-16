"""agents.service_account: the account a connected repository keeps for itself

Revision ID: b8d0f2a4c6e9
Revises: a7c9e1b3d5f6
Create Date: 2026-11-15 12:00:00.000000

A connected repository that already names its own service account keeps it,
and this column holds the name so the Deploy step and the agent's page say
the account the agent really runs as. NULL for every row: an agent runs as
the room's own account unless its repository says otherwise.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'b8d0f2a4c6e9'
down_revision = 'a7c9e1b3d5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('agents', sa.Column('service_account', sa.String(length=253), nullable=True))


def downgrade() -> None:
    op.drop_column('agents', 'service_account')
