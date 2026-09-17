"""agent_learnings.said_by -- who stated the learning (learnings rethink)

Revision ID: d2f6b8a4c1e9
Revises: c7d3a9f21b84
"""

import sqlalchemy as sa
from alembic import op

revision = 'd2f6b8a4c1e9'
down_revision = 'c7d3a9f21b84'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_learnings") as batch:
        batch.add_column(sa.Column("said_by", sa.String(length=120), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent_learnings") as batch:
        batch.drop_column("said_by")
