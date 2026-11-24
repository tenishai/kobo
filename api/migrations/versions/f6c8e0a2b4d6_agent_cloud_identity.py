"""agents.cloud_identity: whether the pod runs as the agent's Google account

Revision ID: f6c8e0a2b4d6
Revises: c3e5a7b9d1f2
Create Date: 2026-11-24 19:00:00.000000

NULL for every row: the pod runs as the agent's Google service account, as
it always has. `direct` is an agent whose pod is granted what it needs on
its Kubernetes identity, leaving the Google account as the name the cloud's
log records for its leases (kobo_api.cloud_identity).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'f6c8e0a2b4d6'
down_revision = 'c3e5a7b9d1f2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('agents', sa.Column('cloud_identity', sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column('agents', 'cloud_identity')
