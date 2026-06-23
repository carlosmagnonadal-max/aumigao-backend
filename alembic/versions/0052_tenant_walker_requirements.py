"""tenant walker extra requirements (F3.2)

Revision ID: 0052_tenant_walker_requirements
Revises: 0051_walker_exception_tenant_scope
Create Date: 2026-06-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0052_tenant_walker_requirements"
down_revision = "0051_walker_exception_tenant_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("walker_extra_requirements", sa.JSON(), nullable=True))
    op.add_column("tenant_walker_access", sa.Column("requirements_submitted_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenant_walker_access", "requirements_submitted_at")
    op.drop_column("tenants", "walker_extra_requirements")
