"""BG-6 — checagem automatica de sancoes: veredito minimizado em walker_profiles.

Adiciona 2 colunas ao walker_profiles para guardar SO o veredito da consulta ao
Portal da Transparencia (CEIS/CNEP) — nunca o dossie (minimizacao LGPD):
  - sanctions_check_status: none|clear|hit|error  (default "none")
  - sanctions_checked_at:   datetime da ultima consulta (nullable)

ADITIVA e IDEMPOTENTE (has_column). server_default seguro ("none") => ZERO
regressao; o efeito so aparece com TRANSPARENCIA_API_KEY configurada E a flag de
tenant `background_checks` ligada (ambos default-OFF).

Colunas novas em tabela ja existente (walker_profiles) NAO exigem policy RLS nova.

Revision ID: 0084_walker_sanctions_check
Revises: 0083_money_decimal
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0084_walker_sanctions_check"
down_revision: Union[str, None] = "0083_money_decimal"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "walker_profiles"

_COLUMNS = (
    ("sanctions_check_status", lambda: sa.Column("sanctions_check_status", sa.String(), nullable=False, server_default="none")),
    ("sanctions_checked_at", lambda: sa.Column("sanctions_checked_at", sa.DateTime(), nullable=True)),
)


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    for column_name, column_factory in _COLUMNS:
        if not _has_column(_TABLE, column_name):
            op.add_column(_TABLE, column_factory())


def downgrade() -> None:
    for column_name, _ in _COLUMNS:
        if _has_column(_TABLE, column_name):
            op.drop_column(_TABLE, column_name)
