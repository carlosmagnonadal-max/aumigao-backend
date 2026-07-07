"""0102 — vitrine dos planos recorrentes (curadoria do tenant).

O app do tutor ganhou uma vitrine de planos mensais (home + aba Planos).
Esta migration dá ao tenant o controle editorial via admin-web:
  - featured: entra na vitrine do app (default false = fallback automático
    por custo-benefício, comportamento atual preservado).
  - display_order: ordem dentro da vitrine (menor primeiro).

Idempotente (IF NOT EXISTS via inspect). Funciona em PG e SQLite (ADD COLUMN
com server_default).

Revision ID: 0102_recurring_plans_showcase
Revises: 0101_walks_destination_coords
Create Date: 2026-07-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0102_recurring_plans_showcase"
down_revision: Union[str, None] = "0101_walks_destination_coords"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return _inspector().has_table(name)


def _has_column(table: str, column: str) -> bool:
    if not _has_table(table):
        return False
    existing = {c["name"] for c in _inspector().get_columns(table)}
    return column in existing


def upgrade() -> None:
    if not _has_table("recurring_plans"):
        return
    if not _has_column("recurring_plans", "featured"):
        op.add_column(
            "recurring_plans",
            sa.Column("featured", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )
    if not _has_column("recurring_plans", "display_order"):
        op.add_column(
            "recurring_plans",
            sa.Column("display_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        )


def downgrade() -> None:
    if not _has_table("recurring_plans"):
        return
    for col in ("display_order", "featured"):
        if _has_column("recurring_plans", col):
            op.drop_column("recurring_plans", col)
