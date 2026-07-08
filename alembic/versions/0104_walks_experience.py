"""0104 — persiste a experiência do passeio (xixi/cocô) em colunas reais.

Teste real 08/07: o POST /walker/walks/{id}/experience apenas logava e devolvia
did_pee/did_poop no JSON — nada era persistido, então o tutor perdia os eventos
a cada reload e o registro na finalização do passeador não tinha efeito.

Colunas nullable (NULL = não informado; False = informado como "não fez").

Idempotente (IF NOT EXISTS via inspect). Funciona em PG e SQLite.

Revision ID: 0104_walks_experience
Revises: 0103_meeting_point_discount
Create Date: 2026-07-08
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0104_walks_experience"
down_revision: Union[str, None] = "0103_meeting_point_discount"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "walks"
COLUMNS = ("did_pee", "did_poop")


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
    for column in COLUMNS:
        if not _has_column(TABLE, column):
            op.add_column(TABLE, sa.Column(column, sa.Boolean(), nullable=True))


def downgrade() -> None:
    for column in COLUMNS:
        if _has_column(TABLE, column):
            op.drop_column(TABLE, column)
