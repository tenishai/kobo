"""One audit log for the room (issue #316)

`audit_log`: every room-visible action by a person, the room or an agent,
with the sentence, the outcome, where the request came from and how far the
action landed. Indexed on `at`, `actor`, `action` and `agent_name`, the four
the Audit page filters on. Additive; `admin_audit_events` stays and is
copied into this table once at boot.

Revision ID: c9a1f4d2b7e3
Revises: a3b9c7d1e2f4
Create Date: 2026-10-30 09:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = 'c9a1f4d2b7e3'
down_revision = 'a3b9c7d1e2f4'
branch_labels = None
depends_on = None

_BODY = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        'audit_log',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.Column('actor', sa.String(length=320), nullable=False),
        sa.Column('actor_kind', sa.String(length=16), nullable=False, server_default='person'),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('target_kind', sa.String(length=64), nullable=False),
        sa.Column('target_id', sa.String(length=200), nullable=False),
        sa.Column('agent_name', sa.String(length=63), nullable=True),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('outcome', sa.String(length=16), nullable=False, server_default='ok'),
        sa.Column('source', sa.String(length=32), nullable=False, server_default='panel'),
        sa.Column('request_ip', sa.String(length=64), nullable=True),
        sa.Column('user_agent', sa.String(length=300), nullable=True),
        sa.Column('detail', _BODY, nullable=True),
        sa.Column('landed', sa.String(length=16), nullable=True),
        sa.Column('landed_detail', sa.Text(), nullable=True),
    )
    op.create_index('ix_audit_log_at', 'audit_log', ['at'])
    op.create_index('ix_audit_log_actor', 'audit_log', ['actor'])
    op.create_index('ix_audit_log_action', 'audit_log', ['action'])
    op.create_index('ix_audit_log_agent_name', 'audit_log', ['agent_name'])


def downgrade() -> None:
    op.drop_index('ix_audit_log_agent_name', table_name='audit_log')
    op.drop_index('ix_audit_log_action', table_name='audit_log')
    op.drop_index('ix_audit_log_actor', table_name='audit_log')
    op.drop_index('ix_audit_log_at', table_name='audit_log')
    op.drop_table('audit_log')
