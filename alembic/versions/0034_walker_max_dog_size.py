"""Wave 5 — porte máximo de cão aceito pelo passeador.

walker_profiles: adiciona max_dog_size (String, server_default "Grande").
Default PERMISSIVO: aceita todos os portes => ZERO regressão até configurarem.

Revision ID: 0034_walker_max_dog_size
Revises: 0033_money_numeric
Create Date: 2026-06-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0034_walker_max_dog_size"
down_revision: Union[str, None] = "0033_money_numeric"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "walker_profiles"
_COLUMN = "max_dog_size"


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if not _has_column(_TABLE, _COLUMN):
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.String(), nullable=False, server_default="Grande"),
        )


def downgrade() -> None:
    if _has_column(_TABLE, _COLUMN):
        op.drop_column(_TABLE, _COLUMN)
