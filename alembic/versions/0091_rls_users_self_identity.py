"""RLS: identidade GLOBAL — amplia a policy de `users` para a própria linha (0091).

CONTEXTO (Modelo B — identidade global do usuário):
  O usuário é criado num tenant (users.tenant_id, ex.: "aumigao") mas pode trocar de
  tenant no app via header X-Tenant-Slug (ex.: "pmg"); os vínculos por tenant vivem em
  TenantTutorAccess/TenantWalkerAccess, não em users.tenant_id. Quando o RLS da sessão
  escopa para o tenant B, a policy `tenant_isolation` da tabela `users`
  (0043/0044: '*' OR tenant_id IS NULL OR tenant_id = current_tenant) ESCONDE a linha
  do próprio usuário (tenant A) → get_current_user (app/dependencies/auth.py) faz
  db.get(User, user_id) e recebe None → 401 "Usuario invalido" em TODA request → o app
  desloga o usuário ao trocar de tenant.

FIX:
  Amplia a policy `tenant_isolation` APENAS da tabela `users` para SEMPRE permitir a
  PRÓPRIA linha, via predicado `id::text = current_setting('app.current_user_id', true)`
  (GUC já injetado por app/core/database.py no after_begin; get_current_user grava o
  user_id em session.info ANTES do lookup para o after_begin da 1ª query já enxergá-lo).

  A ampliação NÃO enfraquece o isolamento de DADOS:
    - vale SÓ para a tabela `users` (dados de negócio — pets, walks, payments etc.
      permanecem escopados pelo tenant);
    - vale SÓ para a PRÓPRIA linha (id = current_user_id) — um usuário sob escopo do
      tenant B continua SEM enxergar OUTROS usuários (nem do tenant A nem do B);
    - o guard `NOT IN ('-', '')` impede que sessões sem usuário autenticado
      (current_user_id default '-') ou com GUC vazio casem qualquer linha por esse ramo.

  Espelha o padrão da 0049 (walks: walker-self via app.current_user_id).

PG-only; NO-OP em sqlite. Idempotente (DROP POLICY IF EXISTS + CREATE POLICY).

Revision ID: 0091_rls_users_self_identity
Revises: 0090_tenant_product_highlights
Create Date: 2026-07-03
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0091_rls_users_self_identity"
down_revision: Union[str, None] = "0090_tenant_product_highlights"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_POLICY = "tenant_isolation"

# Predicado ampliado da tabela `users`: escopo de tenant (0044) + PRÓPRIA linha.
# O ramo self-identity é fechado por NOT IN ('-', '') — '-' é o default de sessões
# sem usuário autenticado e '' é o GUC não setado (fail-closed).
_USERS_PREDICATE = (
    "current_setting('app.current_tenant', true) = '*' "
    "OR tenant_id IS NULL "
    "OR tenant_id::text = current_setting('app.current_tenant', true) "
    "OR ("
    "current_setting('app.current_user_id', true) NOT IN ('-', '') "
    "AND id::text = current_setting('app.current_user_id', true)"
    ")"
)

# Policy da 0044 (estado anterior) — usada no downgrade para restaurar exatamente.
_USERS_PREDICATE_PRE = (
    "current_setting('app.current_tenant', true) = '*' "
    "OR tenant_id IS NULL "
    "OR tenant_id::text = current_setting('app.current_tenant', true)"
)


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        # NO-OP em SQLite (CI/testes) — policies são feature exclusiva do PostgreSQL.
        return
    op.execute(f'DROP POLICY IF EXISTS {_POLICY} ON "users"')
    op.execute(
        f'CREATE POLICY {_POLICY} ON "users" '
        f"USING ({_USERS_PREDICATE}) WITH CHECK ({_USERS_PREDICATE})"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    op.execute(f'DROP POLICY IF EXISTS {_POLICY} ON "users"')
    op.execute(
        f'CREATE POLICY {_POLICY} ON "users" '
        f"USING ({_USERS_PREDICATE_PRE}) WITH CHECK ({_USERS_PREDICATE_PRE})"
    )
