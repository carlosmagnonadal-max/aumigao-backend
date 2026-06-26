"""tenant_fiscal_config + payment_provision

Revision ID: 0061_fiscal_provisioning
Revises: 0060_nfse
"""
import sqlalchemy as sa
from alembic import op

revision = "0061_fiscal_provisioning"
down_revision = "0060_nfse"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        "tenant_fiscal_config",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("commission_tax_percent", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("subscription_tax_percent", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("walker_tax_percent", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("iss_percent", sa.Numeric(10, 2), nullable=True),
        sa.Column("municipal_service_code", sa.String(), nullable=True),
        sa.Column("simples_nacional", sa.Boolean(), nullable=True),
        sa.Column("cnae", sa.String(), nullable=True),
        sa.Column("service_description", sa.String(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("uq_tenant_fiscal_config_tenant", "tenant_fiscal_config", ["tenant_id"], unique=True)
    op.create_table(
        "payment_provision",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("payment_id", sa.String(), nullable=False),
        sa.Column("revenue_type", sa.String(), nullable=False),
        sa.Column("walker_gross", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("walker_tax", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("walker_net", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("platform_gross", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("platform_tax", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("platform_net", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("walker_tax_percent_applied", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("platform_tax_percent_applied", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_payment_provision_tenant_id", "payment_provision", ["tenant_id"])
    op.create_index("uq_payment_provision_payment", "payment_provision", ["payment_id"], unique=True)

def downgrade() -> None:
    op.drop_index("uq_payment_provision_payment", table_name="payment_provision")
    op.drop_index("ix_payment_provision_tenant_id", table_name="payment_provision")
    op.drop_table("payment_provision")
    op.drop_index("uq_tenant_fiscal_config_tenant", table_name="tenant_fiscal_config")
    op.drop_table("tenant_fiscal_config")
