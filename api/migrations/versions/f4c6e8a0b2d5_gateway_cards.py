"""gateway_cards and gateway_managed: lanes from the gateway's handler cards

Revision ID: f4c6e8a0b2d5
Revises: e8a3b5c7d9f1
Create Date: 2026-11-19 22:00:00.000000

The room derives what each permission opens on the gateway from the handlers'
own capability cards (issue #524, step 5). `gateway_cards` keeps the last read,
so a gateway that cannot answer never reads as "no handlers".
`gateway_managed` keeps every scope, handler and allow pattern those lanes have
named, so a handler that leaves the cards is still withdrawn from every agent
that held it. Both tables are new. Nothing existing changes.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = 'f4c6e8a0b2d5'
down_revision = 'e8a3b5c7d9f1'
branch_labels = None
depends_on = None

_BODY = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        'gateway_cards',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('cards', _BODY, nullable=False),
        sa.Column('read_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'gateway_managed',
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('value', sa.String(length=200), nullable=False),
        sa.Column('vendor', sa.String(length=64), nullable=False),
        sa.Column('first_seen', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('kind', 'value'),
    )


def downgrade() -> None:
    op.drop_table('gateway_managed')
    op.drop_table('gateway_cards')
