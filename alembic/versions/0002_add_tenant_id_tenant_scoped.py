"""M5 — adiciona tenant_id (nullable + index) nas tabelas tenant-scoped

Passo 1 do M5: APENAS adiciona a coluna `tenant_id` (nullable) e um índice em
cada tabela operacional que, pela spec, deve ser tenant-scoped mas hoje não tem
o campo. É 100% aditivo e reversível:

  - coluna entra NULLABLE → não quebra inserts existentes nem o ORM;
  - sem FK e sem backfill aqui (passos posteriores, após validação);
  - `IF NOT EXISTS` torna a migration idempotente (segura se algo já existir).

IMPORTANTE (protocolo de não-quebra): só adicionar `tenant_id` aos modelos
SQLAlchemy e aos filtros DEPOIS que esta migration estiver aplicada no banco —
caso contrário o ORM referenciaria uma coluna inexistente.

Revision ID: 0002_add_tenant_id_tenant_scoped
Revises: 0001_baseline
Create Date: 2026-06-09
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0002_add_tenant_id_tenant_scoped"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Tabelas "raiz" de cada agregado que precisam de tenant_id próprio.
# (As filhas de complaint — evidences/decisions/status_history/risk_scores —
#  ficam fora: são acessadas via complaint pai, que já carrega o tenant_id.)
_TABLES: tuple[str, ...] = (
    "payments",
    "walk_reviews",
    "walker_reviews",
    "walk_tips",
    "walk_completion_reviews",
    "complaints",
)


def upgrade() -> None:
    for table in _TABLES:
        op.execute(
            f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS tenant_id VARCHAR'
        )
        op.execute(
            f'CREATE INDEX IF NOT EXISTS ix_{table}_tenant_id '
            f'ON {table} (tenant_id)'
        )


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f'DROP INDEX IF EXISTS ix_{table}_tenant_id')
        op.execute(f'ALTER TABLE {table} DROP COLUMN IF EXISTS tenant_id')
