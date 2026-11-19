"""guardrail_rules.breaks: how often a person said the agent broke a rule

Revision ID: e8a3b5c7d9f1
Revises: d7f2a4c6e8b1
Create Date: 2026-11-19 21:00:00.000000

A rule is guidance in the prompt; one that keeps breaking wants a check or a
permission instead. The count, when and by whom, give the Guardrails section
the evidence to say so (issue #524). Zero for every row.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'e8a3b5c7d9f1'
down_revision = 'd7f2a4c6e8b1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'guardrail_rules',
        sa.Column('breaks', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column('guardrail_rules', sa.Column('last_broken_at', sa.DateTime(), nullable=True))
    op.add_column(
        'guardrail_rules', sa.Column('last_broken_by', sa.String(length=320), nullable=True)
    )


def downgrade() -> None:
    op.drop_column('guardrail_rules', 'last_broken_by')
    op.drop_column('guardrail_rules', 'last_broken_at')
    op.drop_column('guardrail_rules', 'breaks')
