"""tenant_saas_subscriptions table

Revision ID: 0059_tenant_saas_subscriptions
Revises: 0058_tenant_billing_columns
"""
import sqlalchemy as sa
from alembic import op

revision = "0059_tenant_saas_subscriptions"
down_revision = "0058_tenant_billing_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_saas_subscriptions",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("plan", sa.String(), nullable=False),
        sa.Column("price", sa.Numeric(10, 2), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("asaas_subscription_id", sa.String(), nullable=True),
        sa.Column("current_period_start", sa.DateTime(), nullable=True),
        sa.Column("current_period_end", sa.DateTime(), nullable=True),
        sa.Column("last_payment_at", sa.DateTime(), nullable=True),
        sa.Column("overdue_since", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_tenant_saas_subscriptions_tenant_id",
        "tenant_saas_subscriptions",
        ["tenant_id"],
    )
    op.create_index(
        "uq_tenant_saas_active",
        "tenant_saas_subscriptions",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_tenant_saas_active", table_name="tenant_saas_subscriptions")
    op.drop_index(
        "ix_tenant_saas_subscriptions_tenant_id",
        table_name="tenant_saas_subscriptions",
    )
    op.drop_table("tenant_saas_subscriptions")
