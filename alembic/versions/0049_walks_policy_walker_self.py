"""Fase 1 Passo 2 — ALTER POLICY walks: passeador vê seus próprios walks em escopo global.

Estende o USING da policy tenant_isolation na tabela walks para incluir
uma cláusula de walker-self: quando app.current_user_id está setado (não
é '-' nem vazio), o passeador pode ver walks onde é walker_id OU
assigned_walker_id — independente do tenant.

Invariante de segurança:
  - WITH CHECK permanece INALTERADO (escrita continua tenant-scoped).
  - Apenas USING é estendido (leitura).
  - O rls_tenant="*" + walker_id filter na query é a primeira barreira;
    esta policy é a segunda (defense-in-depth no banco).

Dialect-aware: executa apenas em PostgreSQL. SQLite (CI/testes) é skip
silencioso, pois o SQLite não implementa RLS — os testes validam a camada
de aplicação (db.info["rls_tenant"] e filtros de query).

A aplicação real desta migration é validada pelo Carlos no Neon
(neondb_owner como dono) — o role "aumigao_app" deve estar ativo e
configurado sem BYPASSRLS para que a policy tenha efeito.

Revision ID: 0049_walks_policy_walker_self
Revises: 0048_walker_multitenant
Create Date: 2026-06-23
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0049_walks_policy_walker_self"
down_revision: Union[str, None] = "0048_walker_multitenant"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ── SQL dos USING clauses ──────────────────────────────────────────────────────

# USING estendido: tenant-scope OR walker-self (Passo 2)
_USING_V2 = """(
  current_setting('app.current_tenant', true) = '*'
  OR tenant_id::text = current_setting('app.current_tenant', true)
  OR (
    current_setting('app.current_user_id', true) NOT IN ('-', '')
    AND (
      walker_id::text = current_setting('app.current_user_id', true)
      OR assigned_walker_id::text = current_setting('app.current_user_id', true)
    )
  )
)"""

# USING original (Passo 1 / 0043): apenas tenant-scope
_USING_V1 = """(
  current_setting('app.current_tenant', true) = '*'
  OR tenant_id::text = current_setting('app.current_tenant', true)
)"""

# WITH CHECK: inalterado (escrita continua tenant-scoped)
_WITH_CHECK_UNCHANGED = """(
  current_setting('app.current_tenant', true) = '*'
  OR tenant_id::text = current_setting('app.current_tenant', true)
)"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite (CI/testes): RLS não existe — skip silencioso.
        return

    bind.execute(
        sa.text(
            f"ALTER POLICY tenant_isolation ON walks "
            f"USING {_USING_V2} "
            f"WITH CHECK {_WITH_CHECK_UNCHANGED}"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # Restaura o USING original (apenas tenant-scope).
    bind.execute(
        sa.text(
            f"ALTER POLICY tenant_isolation ON walks "
            f"USING {_USING_V1} "
            f"WITH CHECK {_WITH_CHECK_UNCHANGED}"
        )
    )
