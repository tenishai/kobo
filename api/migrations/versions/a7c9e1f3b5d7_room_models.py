"""room_models: which models the room offers, and where each comes from (kobo#405)

Revision ID: a7c9e1f3b5d7
Revises: f2a7c4d9e1b3
Create Date: 2026-11-02 12:00:00.000000

One row per model the room knows: discovered from a connected provider
(Vertex AI, Bedrock, a self-hosted endpoint) or seeded from the org
config's `models:` list, offered in New agent only while `enabled`. The
table is created empty; the seed runs in the app at boot
(`kobo_api.room_models.seed`) when the table is empty, so the org list's
price names and tiers and the answering models of any self-hosted row
already connected start enabled and nothing disappears from New agent on
the roll that brings this table.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'a7c9e1f3b5d7'
down_revision = 'f2a7c4d9e1b3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'room_models',
        sa.Column('id', sa.String(length=300), nullable=False),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('source', sa.String(length=64), nullable=True),
        sa.Column('model', sa.String(length=200), nullable=False),
        sa.Column('label', sa.String(length=200), nullable=False),
        sa.Column('location', sa.String(length=64), nullable=True),
        sa.Column('enabled', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('tier', sa.String(length=32), nullable=True),
        sa.Column('family', sa.String(length=64), nullable=True),
        sa.Column('added_by', sa.String(length=320), nullable=False),
        sa.Column('added_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_room_models_provider'), 'room_models', ['provider'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_room_models_provider'), table_name='room_models')
    op.drop_table('room_models')
