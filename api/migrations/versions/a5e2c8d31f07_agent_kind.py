"""agents.kind / run_mode / secret_names (connected-agents design 2026-09-21)

`kind` is what the pipeline selector, the destroy ledger, both read
routes and the UI's tab model each read to tell an agent the room
composed from one a team connected. It is NOT NULL with a server default
of 'composed' because every existing row is exactly that -- the room
built it and owns its repo -- and because the destroy ledger reads this
column to decide whether it may archive a repository, which is not a
question a NULL may answer.

`run_mode` and `secret_names` are recorded only for connected agents: a
pipeline step sees nothing but the row, so "does this code serve HTTP"
and "which Secrets does its namespace hold" have to live on it. run_mode
stays nullable -- a composed agent is never asked -- while secret_names
takes the same not-null-with-a-default shape as `skills` so its readers
iterate a list rather than test for one.

Revision ID: a5e2c8d31f07
Revises: f3c1d7b6a920
Create Date: 2026-09-21 12:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'a5e2c8d31f07'
down_revision = 'f3c1d7b6a920'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'agents',
        sa.Column('kind', sa.String(length=16), nullable=False, server_default='composed'),
    )
    op.add_column('agents', sa.Column('run_mode', sa.String(length=16), nullable=True))
    op.add_column(
        'agents',
        sa.Column('secret_names', sa.JSON(), nullable=False, server_default='[]'),
    )


def downgrade() -> None:
    op.drop_column('agents', 'secret_names')
    op.drop_column('agents', 'run_mode')
    op.drop_column('agents', 'kind')
