"""The admin panel's store: admin_audit_events and admin_settings

Design 2026-10-18 (one runtime and admin panel), Arc 2 data model. Two
additive tables, prefixed `admin_` because SQLite has no schemas. JSON bodies
are JSONB on Postgres, JSON text on SQLite. Nothing existing changes.

Revision ID: a1b2c3d4e5f6
Revises: a8e2c4f6d913
Create Date: 2026-10-18 20:30:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = 'a8e2c4f6d913'
branch_labels = None
depends_on = None

_BODY = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        'admin_audit_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.Column('actor', sa.String(length=320), nullable=False),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('object_type', sa.String(length=64), nullable=False),
        sa.Column('object_id', sa.String(length=200), nullable=False),
        sa.Column('landed', sa.String(length=16), nullable=False, server_default='pending'),
        sa.Column('landed_detail', sa.Text(), nullable=True),
        sa.Column('before', _BODY, nullable=True),
        sa.Column('after', _BODY, nullable=True),
    )
    op.create_index('ix_admin_audit_events_at', 'admin_audit_events', ['at'])
    op.create_table(
        'admin_settings',
        sa.Column('key', sa.String(length=120), primary_key=True),
        sa.Column('value', _BODY, nullable=False),
        sa.Column('spec_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('updated_by', sa.String(length=320), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('admin_settings')
    op.drop_index('ix_admin_audit_events_at', table_name='admin_audit_events')
    op.drop_table('admin_audit_events')
