"""White label — preço de passeio compartilhado por duração (30/45/60 min)

Aditivo e reversível. Adiciona price_30/45/60 em tenant_shared_walk_configs.
Mantém price_per_pet para compatibilidade.

Revision ID: 0027_shared_walk_duration_pricing
Revises: 0026_individual_walk_pricing
Create Date: 2026-06-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0027_shared_walk_duration_pricing"
down_revision: Union[str, None] = "0026_individual_walk_pricing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "tenant_shared_walk_configs"


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}
    if column.name not in existing:
        op.add_column(table, column)


def upgrade() -> None:
    _add_column_if_missing(
        _TABLE,
        sa.Column("price_30", sa.Float(), nullable=False, server_default="29.90"),
    )
    _add_column_if_missing(
        _TABLE,
        sa.Column("price_45", sa.Float(), nullable=False, server_default="39.50"),
    )
    _add_column_if_missing(
        _TABLE,
        sa.Column("price_60", sa.Float(), nullable=False, server_default="49.90"),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "price_60")
    op.drop_column(_TABLE, "price_45")
    op.drop_column(_TABLE, "price_30")
