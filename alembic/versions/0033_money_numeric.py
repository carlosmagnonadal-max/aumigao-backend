"""Governança de dinheiro — campos monetários Float -> Numeric(12,2).

Aplica NUMERIC(12,2) aos valores em R$ (não aos percentuais): payments.amount/
platform_amount/walker_amount, walks.price, walk_tips.amount. Precisão exata no
Postgres, sem drift de ponto flutuante no armazenamento. Só roda no PostgreSQL —
em SQLite a affinity NUMERIC já é compatível (no-op), e o tipo Money (TypeDecorator)
garante o arredondamento a centavos em qualquer dialeto.

⚠️ Produção: o ALTER COLUMN reescreve a coluna (cast double->numeric, arredonda a 2
casas). Valores de R$ já têm ≤2 casas, mas aplicar em janela de baixa carga.

Revision ID: 0033_money_numeric
Revises: 0032_tenant_walker_access_invite_states
Create Date: 2026-06-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0033_money_numeric"
down_revision: Union[str, None] = "0032_walker_access_invites"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = [
    ("payments", "amount"),
    ("payments", "platform_amount"),
    ("payments", "walker_amount"),
    ("walks", "price"),
    ("walk_tips", "amount"),
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table, col in _COLUMNS:
        op.alter_column(
            table, col,
            type_=sa.Numeric(12, 2),
            existing_type=sa.Float(),
            postgresql_using=f"{col}::numeric(12,2)",
            existing_nullable=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table, col in _COLUMNS:
        op.alter_column(
            table, col,
            type_=sa.Float(),
            existing_type=sa.Numeric(12, 2),
            postgresql_using=f"{col}::double precision",
            existing_nullable=True,
        )
