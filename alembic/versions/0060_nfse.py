"""nfse table — NFS-e (nota fiscal de servico)

Revision ID: 0060_nfse
Revises: 0059_tenant_saas_subscriptions
"""
import sqlalchemy as sa
from alembic import op

revision = "0060_nfse"
down_revision = "0059_tenant_saas_subscriptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "nfse",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("asaas_payment_id", sa.String(), nullable=True),
        sa.Column("subscription_id", sa.String(), nullable=True),
        sa.Column("asaas_invoice_id", sa.String(), nullable=True),
        sa.Column("service_type", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("value", sa.Numeric(10, 2), nullable=False),
        sa.Column("nfse_number", sa.String(), nullable=True),
        sa.Column("pdf_url", sa.String(), nullable=True),
        sa.Column("xml_url", sa.String(), nullable=True),
        sa.Column("validation_code", sa.String(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("external_reference", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_nfse_tenant_id", "nfse", ["tenant_id"])
    op.create_index("ix_nfse_asaas_payment_id", "nfse", ["asaas_payment_id"])
    op.create_index("ix_nfse_asaas_invoice_id", "nfse", ["asaas_invoice_id"])


def downgrade() -> None:
    op.drop_index("ix_nfse_asaas_invoice_id", table_name="nfse")
    op.drop_index("ix_nfse_asaas_payment_id", table_name="nfse")
    op.drop_index("ix_nfse_tenant_id", table_name="nfse")
    op.drop_table("nfse")
