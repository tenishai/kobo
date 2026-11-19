"""guardrail_rules: where each of an agent's guardrail rules came from

Revision ID: d7f2a4c6e8b1
Revises: c1e3a5b7d9f2
Create Date: 2026-11-19 18:00:00.000000

An agent's rules live in its repository as bundle/guardrails.md, one per line.
This table holds what the file cannot say: each rule's source (a person, the
persona, a skill, a correction) and who added or last edited it (issue #524).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'd7f2a4c6e8b1'
down_revision = 'c1e3a5b7d9f2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'guardrail_rules',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('agent_name', sa.String(length=100), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('source_kind', sa.String(length=16), nullable=False),
        sa.Column('source_ref', sa.String(length=200), nullable=True),
        sa.Column('added_by', sa.String(length=320), nullable=True),
        sa.Column('added_at', sa.DateTime(), nullable=False),
        sa.Column('edited_by', sa.String(length=320), nullable=True),
        sa.Column('edited_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_guardrail_rules_agent_name'), 'guardrail_rules', ['agent_name'], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_guardrail_rules_agent_name'), table_name='guardrail_rules')
    op.drop_table('guardrail_rules')
