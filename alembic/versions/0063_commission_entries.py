"""commission_entries: ledger de comissão medida do tenant (Fase 1)

Revision ID: 0063_commission_entries
Revises: 0062_tax_regime
"""
import sqlalchemy as sa
from alembic import op

revision = "0063_commission_entries"
down_revision = "0062_tax_regime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "commission_entries",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("walk_id", sa.String(), nullable=False),
        sa.Column("period", sa.String(), nullable=False),
        sa.Column("walk_price", sa.Float(), nullable=False),
        sa.Column("commission_percent", sa.Float(), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("is_network", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(), nullable=False, server_default="accrued"),
        sa.Column("asaas_payment_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("billed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint("uq_commission_entries_walk_id", "commission_entries", ["walk_id"])
    op.create_index("ix_commission_entries_tenant_id", "commission_entries", ["tenant_id"])
    op.create_index("ix_commission_entries_period", "commission_entries", ["period"])
    op.create_index("ix_commission_entries_status", "commission_entries", ["status"])
    op.create_index(
        "ix_commission_entries_tenant_period_status",
        "commission_entries", ["tenant_id", "period", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_commission_entries_tenant_period_status", table_name="commission_entries")
    op.drop_index("ix_commission_entries_status", table_name="commission_entries")
    op.drop_index("ix_commission_entries_period", table_name="commission_entries")
    op.drop_index("ix_commission_entries_tenant_id", table_name="commission_entries")
    op.drop_constraint("uq_commission_entries_walk_id", "commission_entries", type_="unique")
    op.drop_table("commission_entries")
