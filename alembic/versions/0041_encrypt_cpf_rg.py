"""Adiciona colunas de blind-index para CPF (migration 0041 — ADITIVA).

Parte 1 de 2 do achado #6 (cifrar CPF/RG em repouso). Esta migration é
PURAMENTE ADITIVA e segura de aplicar ANTES do deploy do código novo:
o código antigo ignora a coluna nova.

Adiciona:
- tutor_profiles.cpf_bidx   (String, nullable, index)
- walker_profiles.cpf_bidx  (String, nullable, index)

O backfill (cifrar valores existentes + preencher o bidx) está na migration
SEGUINTE (0042_backfill_encrypt_cpf_rg), que deve rodar DEPOIS do deploy do
código tolerante — senão o código antigo no ar leria cifra como CPF.

Revision ID: 0041_encrypt_cpf_rg
Revises: 0040_bg_check_provider
Create Date: 2026-06-20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0041_encrypt_cpf_rg"
down_revision: Union[str, None] = "0040_bg_check_provider"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _has_index(table: str, index_name: str) -> bool:
    return index_name in {
        idx["name"] for idx in sa.inspect(op.get_bind()).get_indexes(table)
    }


def upgrade() -> None:
    for table in ("tutor_profiles", "walker_profiles"):
        if not _has_table(table):
            continue
        if not _has_column(table, "cpf_bidx"):
            op.add_column(table, sa.Column("cpf_bidx", sa.String(), nullable=True))
        index_name = f"ix_{table}_cpf_bidx"
        if not _has_index(table, index_name):
            op.create_index(index_name, table, ["cpf_bidx"])


def downgrade() -> None:
    for table in ("walker_profiles", "tutor_profiles"):
        if not _has_table(table):
            continue
        index_name = f"ix_{table}_cpf_bidx"
        if _has_index(table, index_name):
            op.drop_index(index_name, table_name=table)
        if _has_column(table, "cpf_bidx"):
            op.drop_column(table, "cpf_bidx")
