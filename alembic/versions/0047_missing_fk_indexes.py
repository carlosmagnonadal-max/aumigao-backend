"""M-03 — índices FK ausentes: walks.pet_id.

Verificação sistemática das colunas FK candidatas:

  walks(pet_id)                        → SEM index=True no model → ADICIONADO
  shared_walks(created_by_tutor_id)    → index=True já no model  → PULADO
  complaints(target_pet_id)            → index=True já no model  → PULADO
  complaint_evidences(created_by_id)   → index=True já no model  → PULADO
  tutor_subscriptions(tutor_id)        → index=True já no model  → PULADO
  coupon_redemptions(user_id)          → index=True já no model  → PULADO

O único índice ausente em produção é ix_walks_pet_id. As demais colunas já
possuem index=True declarado no ORM e o índice correspondente foi criado pela
migration de baseline ou pelo create_all dos testes.

Idempotente: CREATE INDEX IF NOT EXISTS (seguro re-aplicar em Neon SQL Editor).
Nome bate com o padrão SQLAlchemy (ix_<table>_<coluna>) — evita que um
autogenerate futuro tente recriar/derrubar.

Revision ID: 0047_missing_fk_indexes
Revises: 0046_chat_participants_rls
Create Date: 2026-06-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0047_missing_fk_indexes"
down_revision: Union[str, None] = "0046_chat_participants_rls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# índice → (tabela, coluna)
_INDEXES: dict[str, tuple[str, str]] = {
    "ix_walks_pet_id": ("walks", "pet_id"),
}


def _existing_indexes(table: str) -> set[str]:
    return {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    for name, (table, column) in _INDEXES.items():
        existing = _existing_indexes(table)
        if name not in existing:
            op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({column})")


def downgrade() -> None:
    for name, (table, _) in _INDEXES.items():
        existing = _existing_indexes(table)
        if name in existing:
            op.execute(f"DROP INDEX IF EXISTS {name}")
