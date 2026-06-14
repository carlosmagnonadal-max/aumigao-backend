"""White label — preço de passeio individual por tenant (30/45/60 min)

Aditivo e reversível. Espelha tenant_shared_walk_configs: 1 linha por tenant.

Revision ID: 0026_individual_walk_pricing
Revises: 0025_money_fields
Create Date: 2026-06-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0026_individual_walk_pricing"
down_revision: Union[str, None] = "0025_money_fields"
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
    _create_table(
        "tenant_individual_walk_pricing",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("price_30", sa.Float(), nullable=False, server_default="36.90"),
        sa.Column("price_45", sa.Float(), nullable=False, server_default="49.90"),
        sa.Column("price_60", sa.Float(), nullable=False, server_default="62.90"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id"),
    )
    _create_index("ix_tenant_individual_walk_pricing_tenant_id", "tenant_individual_walk_pricing", ["tenant_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_tenant_individual_walk_pricing_tenant_id", table_name="tenant_individual_walk_pricing")
    op.drop_table("tenant_individual_walk_pricing")
