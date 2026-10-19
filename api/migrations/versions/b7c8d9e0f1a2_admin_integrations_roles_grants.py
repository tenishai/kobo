"""Admin panel: integrations, roles, role groups, grants

Design 2026-10-18, Arc 2 data model, step 3. Four additive tables with the
`admin_` prefix and JSON bodies (JSONB on Postgres) under a spec_version.
Nothing existing changes.

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-10-18 22:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = 'b7c8d9e0f1a2'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None

_BODY = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        'admin_integrations',
        sa.Column('id', sa.String(length=64), primary_key=True),
        sa.Column('vendor', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('base_access', sa.String(length=16), nullable=False, server_default='read'),
        sa.Column('state', sa.String(length=16), nullable=False, server_default='pending'),
        sa.Column('state_detail', sa.Text(), nullable=True),
        sa.Column('credential_ref', sa.String(length=300), nullable=True),
        sa.Column('spec', _BODY, nullable=False),
        sa.Column('spec_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('created_by', sa.String(length=320), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_table(
        'admin_roles',
        sa.Column('id', sa.String(length=64), primary_key=True),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('builtin', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('spec', _BODY, nullable=False),
        sa.Column('spec_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_table(
        'admin_role_groups',
        sa.Column('role_id', sa.String(length=64), sa.ForeignKey('admin_roles.id'), primary_key=True),
        sa.Column('group_name', sa.String(length=120), primary_key=True),
    )
    op.create_table(
        'admin_grants',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('agent_name', sa.String(length=100), nullable=False),
        sa.Column('integration_id', sa.String(length=64), sa.ForeignKey('admin_integrations.id'), nullable=False),
        sa.Column('access', sa.String(length=16), nullable=False),
        sa.Column('state', sa.String(length=16), nullable=False, server_default='pending'),
        sa.Column('state_detail', sa.Text(), nullable=True),
        sa.Column('spec', _BODY, nullable=False),
        sa.Column('spec_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('requested_by', sa.String(length=320), nullable=False),
        sa.Column('decided_by', sa.String(length=320), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('agent_name', 'integration_id'),
    )
    op.create_index('ix_admin_grants_agent_name', 'admin_grants', ['agent_name'])


def downgrade() -> None:
    op.drop_index('ix_admin_grants_agent_name', table_name='admin_grants')
    op.drop_table('admin_grants')
    op.drop_table('admin_role_groups')
    op.drop_table('admin_roles')
    op.drop_table('admin_integrations')
