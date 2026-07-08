"""0103 — desconto "levar até o ponto de encontro" no pricing individual.

Decisão de preço 07/07/2026: buscar em casa é o padrão do produto (embutido na
âncora); quando o TUTOR leva o pet até o ponto de encontro, o tenant pode dar um
desconto flat em R$ (default 0 = comportamento atual preservado). Aplicado
server-side no POST /walks, só na modalidade standard.

Idempotente (IF NOT EXISTS via inspect). Funciona em PG e SQLite (ADD COLUMN
com server_default).

Revision ID: 0103_meeting_point_discount
Revises: 0102_recurring_plans_showcase
Create Date: 2026-07-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0103_meeting_point_discount"
down_revision: Union[str, None] = "0102_recurring_plans_showcase"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "tenant_individual_walk_pricing"
COLUMN = "meeting_point_discount"


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
    if not _has_table(TABLE):
        return
    if not _has_column(TABLE, COLUMN):
        op.add_column(
            TABLE,
            sa.Column(
                COLUMN,
                sa.Numeric(12, 2),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )


def downgrade() -> None:
    if _has_column(TABLE, COLUMN):
        op.drop_column(TABLE, COLUMN)
