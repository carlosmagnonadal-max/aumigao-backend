"""0099 — RLS de support_tickets com ramo self-identity do autor.

A tabela `support_tickets` já recebe a policy `tenant_isolation` padrão da casa
(0043/0044 — base tenant-scoped com NULL allowance). Esta migration AMPLIA
essa policy para incluir o ramo de identidade do autor:

  - dono da linha (user_id == app.current_user_id) sempre vê os PRÓPRIOS tickets,
    independente do escopo de tenant da sessão.

Por que:
  - O backend já filtra por user_id na rota GET /support-tickets/me
    (support_tickets.py:user_list_my_tickets), mas a RLS é a rede de segurança
    contra acesso direto ao DB (script de manutenção, nova rota futura que
    esqueça o filtro, dump de dados etc.).
  - Sem esse ramo, uma query SQL crua `SELECT * FROM support_tickets WHERE
    user_id = ?` ainda retorna linhas porque o filtro de tenant não as esconde
    (tenant_id confere), MAS se o script rodar sob um escopo de tenant errado
    (ex.: rotina de migração de tenant), o autor perde acesso aos seus próprios
    tickets. O ramo self-identity é o piso de segurança.

Não enfraquece o isolamento de DADOS:
  - vale SÓ para a PRÓPRIA linha (user_id = current_user_id) — um usuário
    sob escopo do tenant B continua SEM enxergar tickets de outros usuários;
  - o guard `NOT IN ('-', '')` impede que sessões sem usuário autenticado
    casem qualquer linha por esse ramo.

Espelha o padrão da 0091 (users self-identity) e 0098 (walker_indications).

PG-only; NO-OP em SQLite (CI/testes). Idempotente (DROP POLICY IF EXISTS +
CREATE POLICY).

Revision ID: 0099_rls_support_tickets_user_own
Revises: 0098_walker_indications
Create Date: 2026-07-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0099_rls_support_tickets_user_own"
down_revision: Union[str, None] = "0098_walker_indications"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_POLICY = "tenant_isolation"

# Predicado da policy: base da casa (0090) + ramo self-identity do autor.
_BASE = (
    "current_setting('app.current_tenant', true) = '*' "
    "OR tenant_id IS NULL "
    "OR tenant_id::text = current_setting('app.current_tenant', true)"
)
_PREDICATE_NEW = (
    _BASE
    + " OR ("
    "current_setting('app.current_user_id', true) NOT IN ('-', '') "
    "AND user_id::text = current_setting('app.current_user_id', true)"
    ")"
)


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        # NO-OP em SQLite (CI/testes) — policies são feature exclusiva do PostgreSQL.
        return
    conn.execute(sa.text('ALTER TABLE "support_tickets" ENABLE ROW LEVEL SECURITY'))
    conn.execute(sa.text(f'DROP POLICY IF EXISTS {_POLICY} ON "support_tickets"'))
    conn.execute(
        sa.text(
            f'CREATE POLICY {_POLICY} ON "support_tickets" '
            f"USING ({_PREDICATE_NEW}) "
            f"WITH CHECK ({_PREDICATE_NEW})"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    conn.execute(sa.text(f'DROP POLICY IF EXISTS {_POLICY} ON "support_tickets"'))
    conn.execute(
        sa.text(
            f'CREATE POLICY {_POLICY} ON "support_tickets" '
            f"USING ({_BASE}) "
            f"WITH CHECK ({_BASE})"
        )
    )
