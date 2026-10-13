"""agent_system_tasks: how each system task last went per agent (issue #142)

A new table, so no backfill: an agent has no row until the next nightly
sweep runs for it.

Revision ID: a8e2c4f6d913
Revises: e5a9c3f17b20
Create Date: 2026-10-13 16:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'a8e2c4f6d913'
down_revision = 'e5a9c3f17b20'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'agent_system_tasks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('agent_name', sa.String(length=100), nullable=False),
        sa.Column('task', sa.String(length=32), nullable=False),
        sa.Column('last_run_at', sa.DateTime(), nullable=False),
        sa.Column('last_success_at', sa.DateTime(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('consecutive_failures', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('agent_name', 'task'),
    )


def downgrade() -> None:
    op.drop_table('agent_system_tasks')
