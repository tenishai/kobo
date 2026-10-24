"""MCP: redeemed authorization codes and per-person revocations

Issues #196 and #197. Two small additive tables: one remembers which
authorization codes were already exchanged (the code's jti is the key, so a
replay fails the insert); the other records a person whose MCP access the
room cut off, read by the access-token verifier.

Revision ID: c4d5e6f7a8b9
Revises: b7c8d9e0f1a2
Create Date: 2026-10-24 23:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'c4d5e6f7a8b9'
down_revision = 'b7c8d9e0f1a2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'mcp_redeemed_codes',
        sa.Column('jti', sa.String(length=64), primary_key=True),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('family_id', sa.String(length=64), nullable=False),
        sa.Column('redeemed_at', sa.DateTime(), nullable=False),
    )
    op.create_table(
        'mcp_revocations',
        sa.Column('email', sa.String(length=255), primary_key=True),
        sa.Column('revoked_at', sa.DateTime(), nullable=False),
        sa.Column('revoked_by', sa.String(length=320), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('mcp_revocations')
    op.drop_table('mcp_redeemed_codes')
