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


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _create_table(name: str, *args, **kw) -> None:
    # Idempotente: projeto usa schema-ensure (create_all) -> so cria se faltar.
    if not _has_table(name):
        op.create_table(name, *args, **kw)


def _create_index(index_name: str, table_name: str, columns, **kw) -> None:
    if not _has_table(table_name):
        return
    existing = {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(table_name)}
    if index_name not in existing:
        op.create_index(index_name, table_name, columns, **kw)


def upgrade() -> None:
    op.execute("ALTER TABLE walks ADD COLUMN IF NOT EXISTS modality VARCHAR DEFAULT 'standard'")
    op.execute("ALTER TABLE walks ADD COLUMN IF NOT EXISTS destination TEXT DEFAULT ''")

    _create_table(
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
    _create_index("ix_tenant_pet_tour_configs_tenant_id", "tenant_pet_tour_configs", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_tenant_pet_tour_configs_tenant_id", table_name="tenant_pet_tour_configs")
    op.drop_table("tenant_pet_tour_configs")
    op.execute("ALTER TABLE walks DROP COLUMN IF EXISTS destination")
    op.execute("ALTER TABLE walks DROP COLUMN IF EXISTS modality")
