"""agents.avatar_version and agents.avatar_small: portraits a browser can keep (2026-11-20)

Revision ID: c3e5a7b9d1f2
Revises: f4c6e8a0b2d5
Create Date: 2026-11-20 23:00:00.000000

Every card on the dashboard loaded its agent's full picture, a 1024px PNG of
about a megabyte, on every page load, with nothing to say whether it had
changed. `avatar_version` is a short digest of the picture's bytes
(`kobo_api.avatars.portrait_version`) and `avatar_small` its 160px WebP copy;
the page asks for the small copy by version and the browser keeps it until
the picture changes. Versions are filled here for the pictures already kept;
small copies are made the first time each one is asked for.
"""
from __future__ import annotations

import base64
import hashlib

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'c3e5a7b9d1f2'
down_revision = 'f4c6e8a0b2d5'
branch_labels = None
depends_on = None

_agents = sa.table(
    'agents',
    sa.column('id', sa.Integer()),
    sa.column('avatar_png', sa.Text()),
    sa.column('avatar_version', sa.String(length=16)),
)


def upgrade() -> None:
    op.add_column('agents', sa.Column('avatar_version', sa.String(length=16), nullable=True))
    op.add_column('agents', sa.Column('avatar_small', sa.Text(), nullable=True))
    bind = op.get_bind()
    ids = bind.execute(sa.select(_agents.c.id).where(_agents.c.avatar_png.is_not(None))).scalars().all()
    # One picture at a time: each is about a megabyte of base64.
    for agent_id in ids:
        stored = bind.execute(
            sa.select(_agents.c.avatar_png).where(_agents.c.id == agent_id)
        ).scalar_one()
        version = hashlib.sha256(base64.b64decode(stored)).hexdigest()[:12]
        bind.execute(
            _agents.update().where(_agents.c.id == agent_id).values(avatar_version=version)
        )


def downgrade() -> None:
    op.drop_column('agents', 'avatar_small')
    op.drop_column('agents', 'avatar_version')
