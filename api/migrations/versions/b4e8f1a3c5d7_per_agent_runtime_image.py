"""agents.runtime_image: per-agent runtime selection (IE-4526)

Nullable on purpose: None means the org-wide default, resolved at
compose/re-pin time rather than stored -- default-runtime agents keep
following the org pin, and every pre-existing row is exactly that case.

Revision ID: b4e8f1a3c5d7
Revises: 8c85bb7c076a
Create Date: 2026-09-08 23:30:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'b4e8f1a3c5d7'
down_revision = '8c85bb7c076a'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('agents', sa.Column('runtime_image', sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column('agents', 'runtime_image')
