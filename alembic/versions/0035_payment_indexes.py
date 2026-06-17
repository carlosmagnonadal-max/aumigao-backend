"""M-01 — indices em payments.walk_id e payments.provider_payment_id.

O webhook do Asaas (lookup por provider_payment_id) e o calculo de saldo do
walker (lookup por walk_id) faziam full table scan na tabela payments.
Adiciona dois indices NAO-unicos. Pura performance => ZERO regressao.

Idempotente: cria cada indice so se ainda nao existir (seguro re-aplicar e
convive com RUN_STARTUP_SCHEMA_ENSURE / create_all). Nomes batem com os que o
SQLAlchemy gera para `index=True` no model (ix_payments_<coluna>), evitando que
um autogenerate futuro tente recriar/derrubar.

Revision ID: 0035_payment_indexes
Revises: 0034_walker_max_dog_size
Create Date: 2026-06-17
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0035_payment_indexes"
down_revision: Union[str, None] = "0034_walker_max_dog_size"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "payments"
# nome_do_indice -> coluna
_INDEXES = {
    "ix_payments_walk_id": "walk_id",
    "ix_payments_provider_payment_id": "provider_payment_id",
}


def _existing_indexes(table: str) -> set[str]:
    return {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    existing = _existing_indexes(_TABLE)
    for name, column in _INDEXES.items():
        if name not in existing:
            op.create_index(name, _TABLE, [column])


def downgrade() -> None:
    existing = _existing_indexes(_TABLE)
    for name in _INDEXES:
        if name in existing:
            op.drop_index(name, table_name=_TABLE)
