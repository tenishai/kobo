"""repo_access: the agent PR broker's grant ledger (2026-08-27 spec)

Revision ID: a1b7c9d2e4f6
Revises: 2f177d767702
Create Date: 2026-08-27 19:30:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'a1b7c9d2e4f6'
down_revision = '2f177d767702'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('repo_access',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('agent_name', sa.String(length=100), nullable=False),
    sa.Column('repository', sa.String(length=255), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('requested_by', sa.String(length=255), nullable=False),
    sa.Column('decided_by', sa.String(length=255), nullable=True),
    sa.Column('requested_at', sa.DateTime(), nullable=False),
    sa.Column('decided_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('agent_name', 'repository')
    )


def downgrade() -> None:
    op.drop_table('repo_access')
