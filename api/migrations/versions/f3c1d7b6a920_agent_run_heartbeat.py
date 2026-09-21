"""agents.run_heartbeat_at: liveness of the process running the pipeline

Nullable on purpose, with no backfill: NULL means no run has ever beaten for
this row, which is true of every pre-existing row. `sweep_interrupted`
(kobo_api.provisioner) reads a NULL against the row's own created_at, so a
row wedged by an old process is still swept while a provision that started
seconds ago is left alone.

Revision ID: f3c1d7b6a920
Revises: d2f6b8a4c1e9
Create Date: 2026-09-21 12:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'f3c1d7b6a920'
down_revision = 'd2f6b8a4c1e9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('agents', sa.Column('run_heartbeat_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('agents', 'run_heartbeat_at')
