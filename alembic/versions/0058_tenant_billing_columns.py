"""tenant billing columns: asaas_customer_id + suspended_reason

Revision ID: 0058_tenant_billing_columns
Revises: 0057_tutor_subscription_credits_granted
"""
from alembic import op
import sqlalchemy as sa

revision = "0058_tenant_billing_columns"
down_revision = "0057_tutor_subscription_credits_granted"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("tenants", sa.Column("asaas_customer_id", sa.String(), nullable=True))
    op.add_column("tenants", sa.Column("suspended_reason", sa.String(), nullable=True))
    op.create_index("ix_tenants_asaas_customer_id", "tenants", ["asaas_customer_id"])

def downgrade() -> None:
    op.drop_index("ix_tenants_asaas_customer_id", table_name="tenants")
    op.drop_column("tenants", "suspended_reason")
    op.drop_column("tenants", "asaas_customer_id")
