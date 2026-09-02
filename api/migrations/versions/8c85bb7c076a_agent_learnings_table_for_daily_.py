"""agent_learnings table for daily learnings curation

Revision ID: 8c85bb7c076a
Revises: a1b7c9d2e4f6
Create Date: 2026-09-01 18:52:49.141993

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '8c85bb7c076a'
down_revision = 'a1b7c9d2e4f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('agent_learnings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('agent_name', sa.String(length=100), nullable=False),
    sa.Column('day', sa.String(length=10), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('source', sa.String(length=512), nullable=False),
    sa.Column('evidence', sa.Text(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('decided_by', sa.String(length=255), nullable=True),
    sa.Column('decided_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('agent_name', 'content_hash')
    )


def downgrade() -> None:
    op.drop_table('agent_learnings')
