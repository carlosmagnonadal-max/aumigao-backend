"""Fase 1 Passo 1 — Passeador Multi-Tenant: colunas e índice de suporte.

Adiciona às tabelas existentes as colunas necessárias para o mecanismo
multi-tenant de passeadores (decisões PRD docs/multi-tenant-walker/).

Colunas adicionadas:
  tenant_walker_access:
    - commission_percent NUMERIC(5,2) NULL
    - requirements_met   BOOLEAN NOT NULL DEFAULT true
    - initiated_by       VARCHAR(16) NOT NULL DEFAULT 'tenant'
  walker_network_profile:
    - exclusive_tenant_id VARCHAR NULL (FK → tenants.id, só Postgres)
  tenants:
    - network_access_override BOOLEAN NULL
    - network_access_addon    BOOLEAN NOT NULL DEFAULT false

Índice único parcial (Postgres):
  uq_walker_one_active_exclusive ON tenant_walker_access(walker_user_id)
  WHERE status='active' AND access_type='tenant_exclusive'

Estratégia de idempotência:
  - Colunas: inspecionamos com sa.inspect antes de ADD COLUMN IF NOT EXISTS
  - Índice: CREATE UNIQUE INDEX IF NOT EXISTS (Postgres) ou CREATE INDEX IF NOT EXISTS (SQLite)
  - Downgrade: DROP COLUMN IF EXISTS / DROP INDEX IF EXISTS

Dialect-aware:
  - Postgres: ADD COLUMN IF NOT EXISTS, FK inline, índice parcial suportado.
  - SQLite: usa ALTER TABLE ADD COLUMN (sem IF NOT EXISTS nativo → guardado por inspeção),
    sem FK inline de coluna (SQLite ignora FK silenciosamente em ALTER TABLE),
    sem índice parcial WHERE → usa índice simples no downgrade também.

Revision ID: 0048_walker_multitenant
Revises: 0047_missing_fk_indexes
Create Date: 2026-06-23
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0048_walker_multitenant"
down_revision: Union[str, None] = "0047_missing_fk_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ─── helpers ──────────────────────────────────────────────────────────────────


def _dialect() -> str:
    return op.get_bind().dialect.name  # "postgresql" | "sqlite"


def _existing_columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _existing_indexes(table: str) -> set[str]:
    return {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(table)}


def _add_column_if_missing(table: str, col_name: str, col_ddl: str) -> None:
    """ADD COLUMN idempotente: verifica antes de emitir o DDL."""
    if col_name not in _existing_columns(table):
        if _dialect() == "postgresql":
            op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col_name} {col_ddl}")
        else:
            # SQLite: ADD COLUMN sem IF NOT EXISTS — já guardado pela inspeção acima.
            op.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_ddl}")


def _drop_column_if_exists(table: str, col_name: str) -> None:
    if col_name in _existing_columns(table):
        if _dialect() == "postgresql":
            op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {col_name}")
        else:
            # SQLite não suporta DROP COLUMN em versões antigas; Alembic normalmente
            # faz recreate, mas aqui emitimos só se o dialeto suportar.
            try:
                op.execute(f"ALTER TABLE {table} DROP COLUMN {col_name}")
            except Exception:
                pass  # SQLite < 3.35 — ignorar silenciosamente no downgrade


# ─── upgrade ──────────────────────────────────────────────────────────────────


def upgrade() -> None:
    dialect = _dialect()

    # ── tenant_walker_access ─────────────────────────────────────────────────
    _add_column_if_missing(
        "tenant_walker_access",
        "commission_percent",
        "NUMERIC(5,2) NULL",
    )
    _add_column_if_missing(
        "tenant_walker_access",
        "requirements_met",
        "BOOLEAN NOT NULL DEFAULT true" if dialect == "postgresql" else "BOOLEAN NOT NULL DEFAULT 1",
    )
    _add_column_if_missing(
        "tenant_walker_access",
        "initiated_by",
        "VARCHAR(16) NOT NULL DEFAULT 'tenant'",
    )

    # ── walker_network_profile ───────────────────────────────────────────────
    if dialect == "postgresql":
        _add_column_if_missing(
            "walker_network_profile",
            "exclusive_tenant_id",
            "VARCHAR NULL REFERENCES tenants(id)",
        )
    else:
        # SQLite: ADD COLUMN sem FK inline (SQLite ignora FK em ALTER TABLE)
        _add_column_if_missing(
            "walker_network_profile",
            "exclusive_tenant_id",
            "VARCHAR NULL",
        )

    # ── tenants ──────────────────────────────────────────────────────────────
    _add_column_if_missing(
        "tenants",
        "network_access_override",
        "BOOLEAN NULL",
    )
    _add_column_if_missing(
        "tenants",
        "network_access_addon",
        "BOOLEAN NOT NULL DEFAULT false" if dialect == "postgresql" else "BOOLEAN NOT NULL DEFAULT 0",
    )

    # ── índice único parcial ─────────────────────────────────────────────────
    idx_name = "uq_walker_one_active_exclusive"
    if idx_name not in _existing_indexes("tenant_walker_access"):
        if dialect == "postgresql":
            op.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {idx_name} "
                "ON tenant_walker_access(walker_user_id) "
                "WHERE status = 'active' AND access_type = 'tenant_exclusive'"
            )
        else:
            # SQLite não suporta WHERE em índices → índice simples (sem unicidade parcial)
            op.execute(
                f"CREATE INDEX IF NOT EXISTS {idx_name} "
                "ON tenant_walker_access(walker_user_id)"
            )


# ─── downgrade ────────────────────────────────────────────────────────────────


def downgrade() -> None:
    dialect = _dialect()

    # Índice
    idx_name = "uq_walker_one_active_exclusive"
    if idx_name in _existing_indexes("tenant_walker_access"):
        if dialect == "postgresql":
            op.execute(f"DROP INDEX IF EXISTS {idx_name}")
        else:
            op.execute(f"DROP INDEX IF EXISTS {idx_name}")

    # tenants
    _drop_column_if_exists("tenants", "network_access_addon")
    _drop_column_if_exists("tenants", "network_access_override")

    # walker_network_profile
    _drop_column_if_exists("walker_network_profile", "exclusive_tenant_id")

    # tenant_walker_access
    _drop_column_if_exists("tenant_walker_access", "initiated_by")
    _drop_column_if_exists("tenant_walker_access", "requirements_met")
    _drop_column_if_exists("tenant_walker_access", "commission_percent")
