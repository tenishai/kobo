"""repo_access.migrated_at: the broker's rows moved onto GitHub per agent (kobo#365)

Revision ID: e1f2a3b4c5d6
Revises: c9a1f4d2b7e3
Create Date: 2026-11-01 09:00:00.000000

The pull request broker retired; an approved row's repository is moved at
boot onto the agent's child integration under the room's GitHub connection
(`kobo_api.github_access.migrate_repo_access`), and the row is stamped so
no boot moves it twice. Nullable, no default: unstamped means not yet moved.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'e1f2a3b4c5d6'
down_revision = 'c9a1f4d2b7e3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('repo_access', sa.Column('migrated_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('repo_access', 'migrated_at')
