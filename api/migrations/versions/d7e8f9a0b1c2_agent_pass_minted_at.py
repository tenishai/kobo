"""agents.pass_minted_at: when the room last minted the agent's colleague pass

Issue #199. The PR and Jira brokers refuse a pass issued before this instant,
so a re-wire cuts an older pass off here too, not only at the switchboard.
Nullable, no backfill: NULL means no cut, which is what every existing agent
had before.

Revision ID: d7e8f9a0b1c2
Revises: c4d5e6f7a8b9
Create Date: 2026-10-25 00:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'd7e8f9a0b1c2'
down_revision = 'c4d5e6f7a8b9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('agents', sa.Column('pass_minted_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('agents', 'pass_minted_at')
