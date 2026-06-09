"""Sprint 16 (Fase A) — split de receita: tenant_payment_configs + campos no payment

Aditivo e reversível: nova tabela tenant_payment_configs + 3 colunas nullable em
payments (commission_percent, platform_amount, walker_amount).

Revision ID: 0010_payment_split
Revises: 0009_branding_version
Create Date: 2026-06-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0010_payment_split"
down_revision: Union[str, None] = "0009_branding_version"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tenant_payment_configs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False, server_default="asaas"),
        sa.Column("commission_percent", sa.Float(), nullable=False, server_default="20.0"),
        sa.Column("split_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id"),
    )
    op.create_index("ix_tenant_payment_configs_tenant_id", "tenant_payment_configs", ["tenant_id"])

    op.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS commission_percent FLOAT")
    op.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS platform_amount FLOAT")
    op.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS walker_amount FLOAT")


def downgrade() -> None:
    op.execute("ALTER TABLE payments DROP COLUMN IF EXISTS walker_amount")
    op.execute("ALTER TABLE payments DROP COLUMN IF EXISTS platform_amount")
    op.execute("ALTER TABLE payments DROP COLUMN IF EXISTS commission_percent")
    op.drop_index("ix_tenant_payment_configs_tenant_id", table_name="tenant_payment_configs")
    op.drop_table("tenant_payment_configs")
