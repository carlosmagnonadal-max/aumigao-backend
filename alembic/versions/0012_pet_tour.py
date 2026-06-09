"""Onda 1 — Pet Tour: colunas modality/destination no walk + tenant_pet_tour_configs

Aditivo e reversível. Pet Tour é a modalidade especial (busca de carro + destino
escolhido pelo tutor + duração estendida), gated pela feature flag `pet_tour`.

Revision ID: 0012_pet_tour
Revises: 0011_recurring_plans
Create Date: 2026-06-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0012_pet_tour"
down_revision: Union[str, None] = "0011_recurring_plans"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE walks ADD COLUMN IF NOT EXISTS modality VARCHAR DEFAULT 'standard'")
    op.execute("ALTER TABLE walks ADD COLUMN IF NOT EXISTS destination TEXT DEFAULT ''")

    op.create_table(
        "tenant_pet_tour_configs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("base_price", sa.Float(), nullable=False, server_default="149.90"),
        sa.Column("min_duration_minutes", sa.Integer(), nullable=False, server_default="90"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id"),
    )
    op.create_index("ix_tenant_pet_tour_configs_tenant_id", "tenant_pet_tour_configs", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_tenant_pet_tour_configs_tenant_id", table_name="tenant_pet_tour_configs")
    op.drop_table("tenant_pet_tour_configs")
    op.execute("ALTER TABLE walks DROP COLUMN IF EXISTS destination")
    op.execute("ALTER TABLE walks DROP COLUMN IF EXISTS modality")
