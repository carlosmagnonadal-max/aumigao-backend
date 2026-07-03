"""RLS: o tenant enxerga seus MEMBROS via vínculo ativo (0092).

CONTEXTO (Modelo B — membership por vínculo, não por users.tenant_id):
  No white-label multi-tenant o usuário NASCE num tenant (users.tenant_id, ex.:
  "aumigao") mas passa a ser MEMBRO de outros tenants via tabelas de vínculo
  (tenant_tutor_access / tenant_walker_access), não via users.tenant_id.

  A policy `tenant_isolation` da tabela `users` (0044 + self-identity 0091) só
  enxerga: escopo '*', tenant_id NULL, tenant_id == tenant do escopo, ou a
  PRÓPRIA linha via app.current_user_id (GUC do usuário logado). Sob o escopo RLS
  de um tenant B, a linha de um usuário nascido no tenant A — mas VINCULADO ao B —
  fica INVISÍVEL: o GUC current_user_id é o do ADMIN (não o do tutor/walker), então
  o ramo self-identity não casa.

  Consequência em produção (verificada no tenant pmg): a contagem/listagem de
  tutores do tenant (fix 6f6f17b: união nascidos + vinculados via TenantTutorAccess)
  roda, mas o Postgres FILTRA a linha do user vinculado → o JOIN/EXISTS não casa
  nada → 0 tutores vinculados aparecem. O MESMO vale para walkers da REDE
  (tenant_walker_access): o admin do tenant lista WalkerProfile (global, sem
  tenant_id) mas resolve o `users` do walker via db.get(User, ...) — se o walker
  nasceu noutro tenant, o user fica invisível e o walker é descartado como "fake".

FIX (semântica correta do Modelo B — MEMBERSHIP):
  Amplia a policy `tenant_isolation` da tabela `users` para SEMPRE permitir a linha
  de um usuário que seja MEMBRO ATIVO do tenant do escopo, via EXISTS nas tabelas
  de vínculo:
    OR EXISTS (SELECT 1 FROM tenant_tutor_access a
               WHERE a.tutor_user_id = users.id
                 AND a.tenant_id = current_setting('app.current_tenant', true)
                 AND a.status = 'active')
    OR EXISTS (SELECT 1 FROM tenant_walker_access w
               WHERE w.walker_user_id = users.id
                 AND w.tenant_id = current_setting('app.current_tenant', true)
                 AND w.status = 'active')

  Isolamento preservado: os ramos de vínculo casam SÓ para o tenant do escopo
  corrente (a.tenant_id = current_tenant) e SÓ para vínculos ATIVOS. Um user sem
  vínculo ativo com o tenant do escopo continua invisível — nenhum vazamento
  cross-tenant. Não afrouxa dados de negócio (vale só para a tabela `users`).

  Fecha o buraco por construção: como o predicado compara sempre com
  current_tenant, quando o escopo é '*' o primeiro ramo (= '*') já libera tudo e
  os EXISTS não são avaliados (short-circuit). Um user sem vínculo com o tenant do
  escopo não casa nenhum EXISTS.

PERFORMANCE:
  Os EXISTS rodam por linha de `users` em SELECTs sobre a tabela. Cria índices
  COMPOSTOS cobrindo o predicado — (tutor_user_id, tenant_id, status) e
  (walker_user_id, tenant_id, status) — para o planner resolver cada EXISTS por
  index-only scan correlato (sem heap fetch). IF NOT EXISTS (idempotente); as
  colunas individuais já tinham índice (0053/0048), mas o composto é o ideal aqui.

PG-only; NO-OP em sqlite. Idempotente (DROP POLICY IF EXISTS + CREATE POLICY;
CREATE INDEX IF NOT EXISTS). Downgrade restaura EXATAMENTE a policy da 0091 (self-
identity) e dropa os índices criados aqui.

Revision ID: 0092_rls_users_tenant_members
Revises: 0091_rls_users_self_identity
Create Date: 2026-07-03
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0092_rls_users_tenant_members"
down_revision: Union[str, None] = "0091_rls_users_self_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_POLICY = "tenant_isolation"

# Predicado ampliado (0092): 0091 (escopo + NULL + self-identity) + MEMBERSHIP por
# vínculo ativo (tutor e walker). Os ramos de vínculo comparam sempre com o tenant
# do escopo corrente → não vazam para fora do tenant.
_USERS_PREDICATE = (
    "current_setting('app.current_tenant', true) = '*' "
    "OR tenant_id IS NULL "
    "OR tenant_id::text = current_setting('app.current_tenant', true) "
    "OR ("
    "current_setting('app.current_user_id', true) NOT IN ('-', '') "
    "AND id::text = current_setting('app.current_user_id', true)"
    ") "
    "OR EXISTS ("
    "SELECT 1 FROM tenant_tutor_access a "
    "WHERE a.tutor_user_id = users.id "
    "AND a.tenant_id = current_setting('app.current_tenant', true) "
    "AND a.status = 'active'"
    ") "
    "OR EXISTS ("
    "SELECT 1 FROM tenant_walker_access w "
    "WHERE w.walker_user_id = users.id "
    "AND w.tenant_id = current_setting('app.current_tenant', true) "
    "AND w.status = 'active'"
    ")"
)

# Policy da 0091 (estado anterior: escopo + NULL + self-identity) — restaura no downgrade.
_USERS_PREDICATE_PRE = (
    "current_setting('app.current_tenant', true) = '*' "
    "OR tenant_id IS NULL "
    "OR tenant_id::text = current_setting('app.current_tenant', true) "
    "OR ("
    "current_setting('app.current_user_id', true) NOT IN ('-', '') "
    "AND id::text = current_setting('app.current_user_id', true)"
    ")"
)


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        # NO-OP em SQLite (CI/testes) — policies e RLS são feature exclusiva do PostgreSQL.
        return
    # Índices compostos cobrindo os EXISTS da policy (idempotentes).
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tenant_tutor_access_lookup "
        "ON tenant_tutor_access (tutor_user_id, tenant_id, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tenant_walker_access_lookup "
        "ON tenant_walker_access (walker_user_id, tenant_id, status)"
    )
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
    op.execute("DROP INDEX IF EXISTS ix_tenant_walker_access_lookup")
    op.execute("DROP INDEX IF EXISTS ix_tenant_tutor_access_lookup")
