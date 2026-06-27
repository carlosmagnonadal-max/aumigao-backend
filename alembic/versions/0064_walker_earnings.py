"""walker_earnings: ledger-fornecedor do passeador da rede (Fase 2)

Revision ID: 0064_walker_earnings
Revises: 0063_commission_entries
"""
import sqlalchemy as sa
from alembic import op

revision = "0064_walker_earnings"
down_revision = "0063_commission_entries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "walker_earnings",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("walker_id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=True),
        sa.Column("walk_id", sa.String(), nullable=False),
        sa.Column("gross", sa.Float(), nullable=False),
        sa.Column("platform_amount", sa.Float(), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="accrued"),
        sa.Column("accrued_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("payable_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_unique_constraint("uq_walker_earnings_walk_id", "walker_earnings", ["walk_id"])
    op.create_index("ix_walker_earnings_walker_id", "walker_earnings", ["walker_id"])
    op.create_index("ix_walker_earnings_tenant_id", "walker_earnings", ["tenant_id"])
    op.create_index("ix_walker_earnings_status", "walker_earnings", ["status"])


def downgrade() -> None:
    op.drop_index("ix_walker_earnings_status", table_name="walker_earnings")
    op.drop_index("ix_walker_earnings_tenant_id", table_name="walker_earnings")
    op.drop_index("ix_walker_earnings_walker_id", table_name="walker_earnings")
    op.drop_constraint("uq_walker_earnings_walk_id", "walker_earnings", type_="unique")
    op.drop_table("walker_earnings")
