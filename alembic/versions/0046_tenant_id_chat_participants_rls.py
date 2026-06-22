"""Adiciona tenant_id + RLS a protected_chat_messages e shared_walk_participants.

## O que faz

### Coluna tenant_id (DDL)
Adiciona `tenant_id VARCHAR` nullable com FK para tenants e índice em:
  - protected_chat_messages (derivado de walks.tenant_id via walk_id)
  - shared_walk_participants (derivado de shared_walks.tenant_id via shared_walk_id)

### Backfill
Preenche as linhas existentes via JOIN ao pai:
  - protected_chat_messages: JOIN walks ON m.walk_id = w.id
  - shared_walk_participants: JOIN shared_walks ON p.shared_walk_id = sw.id

### RLS
Após o backfill, habilita ENABLE ROW LEVEL SECURITY e cria a policy
`tenant_isolation` IDÊNTICA à 0045 (USING permissivo com NULL; WITH CHECK estrito):

  USING:
    current_setting('app.current_tenant', true) = '*'
    OR tenant_id IS NULL
    OR tenant_id::text = current_setting('app.current_tenant', true)

  WITH CHECK (estrita — fecha buraco de escrita NULL sob sessão de tenant):
    current_setting('app.current_tenant', true) = '*'
    OR tenant_id::text = current_setting('app.current_tenant', true)

## ORDEM DE APPLY OBRIGATÓRIA

⚠️  O código que propaga tenant_id nos INSERTs DEVE ser deployado ANTES que esta
migration seja aplicada em produção. Caso contrário, novos INSERTs com tenant_id=NULL
sob uma sessão de tenant específico falharão na WITH CHECK estrita.

Deploy ordering:
  1. Deploy do código (app/routes/protected_chat.py + app/services/shared_walk_service.py)
     que passa tenant_id=walk.tenant_id / tenant_id=session.tenant_id nos INSERTs.
  2. Aplicar esta migration (alembic upgrade 0046_tenant_id_chat_participants_rls).
     O backfill cobre as linhas existentes; novos INSERTs já chegam com tenant_id.

## PG-only / idempotente

NO-OP em SQLite (CI/testes — RLS é exclusivo do PostgreSQL).
Idempotente: ADD COLUMN IF NOT EXISTS + DROP POLICY IF EXISTS + CREATE.

Revision ID: 0046_tenant_id_chat_participants_rls
Revises: 0045_rls_harden_with_check
Create Date: 2026-06-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0046_tenant_id_chat_participants_rls"
down_revision: Union[str, None] = "0045_rls_harden_with_check"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_POLICY = "tenant_isolation"

# Mantém a leitura de linhas com tenant_id NULL (uploads anônimos, rows legadas).
_USING = (
    "current_setting('app.current_tenant', true) = '*' "
    "OR tenant_id IS NULL "
    "OR tenant_id::text = current_setting('app.current_tenant', true)"
)

# Estrita: apenas sessões '*' (global/super_admin) podem gravar NULL.
_WITH_CHECK_STRICT = (
    "current_setting('app.current_tenant', true) = '*' "
    "OR tenant_id::text = current_setting('app.current_tenant', true)"
)

# Policy permissiva para o downgrade (espelha 0044).
_PREDICATE_PERMISSIVE = (
    "current_setting('app.current_tenant', true) = '*' "
    "OR tenant_id IS NULL "
    "OR tenant_id::text = current_setting('app.current_tenant', true)"
)

_TABLES = [
    "protected_chat_messages",
    "shared_walk_participants",
]


def upgrade() -> None:
    conn = op.get_bind()

    # --- DDL: ADD COLUMN (ambos os dialetos; FK e índice apenas em PG) ----------

    insp = sa.inspect(conn)
    is_pg = conn.dialect.name == "postgresql"

    for table in _TABLES:
        existing_cols = {c["name"] for c in insp.get_columns(table)}
        if "tenant_id" not in existing_cols:
            if is_pg:
                op.execute(
                    f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS '
                    f"tenant_id VARCHAR REFERENCES tenants(id)"
                )
                op.execute(
                    f'CREATE INDEX IF NOT EXISTS "ix_{table}_tenant_id" '
                    f'ON "{table}" (tenant_id)'
                )
            else:
                # SQLite: ADD COLUMN sem FK (SQLite não suporta ADD CONSTRAINT).
                op.execute(f'ALTER TABLE "{table}" ADD COLUMN tenant_id VARCHAR')

    if not is_pg:
        # RLS é exclusivo do PostgreSQL — NO-OP em SQLite.
        return

    # --- Backfill: propaga tenant_id do pai ----------------------------------------

    # protected_chat_messages ← walks.tenant_id via walk_id
    op.execute(
        """
        UPDATE protected_chat_messages m
        SET tenant_id = w.tenant_id
        FROM walks w
        WHERE m.walk_id = w.id
          AND m.tenant_id IS NULL
          AND w.tenant_id IS NOT NULL
        """
    )

    # shared_walk_participants ← shared_walks.tenant_id via shared_walk_id
    op.execute(
        """
        UPDATE shared_walk_participants p
        SET tenant_id = sw.tenant_id
        FROM shared_walks sw
        WHERE p.shared_walk_id = sw.id
          AND p.tenant_id IS NULL
          AND sw.tenant_id IS NOT NULL
        """
    )

    # --- RLS: ENABLE + CREATE policy -----------------------------------------------

    for table in _TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'DROP POLICY IF EXISTS {_POLICY} ON "{table}"')
        op.execute(
            f'CREATE POLICY {_POLICY} ON "{table}" '
            f"USING ({_USING}) "
            f"WITH CHECK ({_WITH_CHECK_STRICT})"
        )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    # Remove a policy e desabilita RLS (mantém a coluna — seguro para dados).
    for table in _TABLES:
        op.execute(f'DROP POLICY IF EXISTS {_POLICY} ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')

    # Remove a coluna (downgrade completo — dados de tenant_id são perdidos;
    # aceito pois o downgrade volta para antes do RLS nas duas tabelas).
    for table in _TABLES:
        insp = sa.inspect(conn)
        existing_cols = {c["name"] for c in insp.get_columns(table)}
        if "tenant_id" in existing_cols:
            op.execute(
                f'DROP INDEX IF EXISTS "ix_{table}_tenant_id"'
            )
            op.execute(
                f'ALTER TABLE "{table}" DROP COLUMN IF EXISTS tenant_id'
            )
