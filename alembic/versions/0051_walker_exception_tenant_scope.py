"""walker availability exception tenant scope

Revision ID: 0051_walker_exception_tenant_scope
Revises: 0050_walker_availability_exceptions
Create Date: 2026-06-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0051_walker_exception_tenant_scope"
down_revision = "0050_walker_availability_exceptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "walker_availability_exceptions",
        sa.Column("tenant_id", sa.String(), nullable=True),
    )
    op.create_foreign_key(
        "fk_wae_tenant_id", "walker_availability_exceptions", "tenants",
        ["tenant_id"], ["id"],
    )
    op.create_index("ix_wae_tenant_id", "walker_availability_exceptions", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_wae_tenant_id", table_name="walker_availability_exceptions")
    op.drop_constraint("fk_wae_tenant_id", "walker_availability_exceptions", type_="foreignkey")
    op.drop_column("walker_availability_exceptions", "tenant_id")
