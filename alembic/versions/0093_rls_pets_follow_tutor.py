"""RLS: o PET SEGUE O TUTOR entre tenants vinculados (0093).

CONTEXTO ("Pets seguem o tutor" — decisão do dono do produto):
  O pet pertence ao TUTOR, não ao tenant. `pets.tenant_id` passa a significar só
  "tenant de ORIGEM" (onde o pet foi cadastrado) — sem mudança de schema. A ficha e
  a SAÚDE do pet devem ficar visíveis em QUALQUER tenant onde o tutor tem vínculo
  ATIVO (tenant_tutor_access.status='active'): staff/admin e walker desse tenant
  precisam disso pro briefing do passeio.

  Historico OPERACIONAL continua POR TENANT:
    - walk_observations e pet_profile_configs NÃO mudam (ficam isolados por tenant);
    - em pet_timeline_events os eventos de origem operacional do tenant
      (event_type IN ('walk_observation','tenant_note')) NÃO seguem; os demais
      (diary, health_note, vaccine, weight, medication, self_walk, birthday,
      custom …) seguem o tutor.

FIX (RLS — amplia a policy `tenant_isolation`):
  pets: além dos 3 ramos padrão (escopo '*' / tenant_id NULL / tenant_id == escopo),
  ganha o ramo DONO (tutor_id == app.current_user_id, guard NOT IN ('-','') no padrão
  da 0091) e o ramo VÍNCULO ATIVO (EXISTS em tenant_tutor_access para o tutor do pet
  no tenant do escopo corrente — padrão 0092). Assim o pet do tutor aparece sob o
  escopo de qualquer tenant vinculado, sem vazar para tenants sem vínculo.

  Satélites de SAÚDE (pet_health_records, pet_reminders, pet_share_links,
  pet_self_walks): mesmos 3 ramos padrão + ramo DONO e ramo VÍNCULO resolvidos por
  JOIN com `pets` (via <tabela>.pet_id). A subquery em `pets` é avaliada SOB a policy
  de pets — que, com a policy nova, já libera as mesmas linhas nos mesmos casos, então
  é consistente e sem recursão proibida (a policy de pets NÃO referencia satélites).

  pet_timeline_events: idem satélites, mas os ramos DONO e VÍNCULO carregam a condição
  extra `event_type NOT IN ('walk_observation','tenant_note')` — os eventos
  operacionais gerados por walker/tenant ficam presos ao tenant de origem (não seguem).

  Isolamento preservado: os ramos de vínculo comparam sempre com current_tenant e SÓ
  casam vínculo ATIVO; o ramo dono é fechado por NOT IN ('-',''). Um tutor/pet sem
  vínculo ativo com o tenant do escopo continua invisível — nenhum vazamento.

  TRADE-OFF DOCUMENTADO (WITH CHECK): o ramo de vínculo valida contra o tenant da
  SESSÃO, não contra o tenant_id da linha — necessário para o UPDATE legítimo do pet
  seguido (linha mantém tenant_id de origem sob sessão de outro tenant). Com vínculo
  ativo, o RLS sozinho não impede gravar tenant_id de terceiro; essa defesa é da
  camada de aplicação (PetCreate/PetUpdate NÃO expõem tenant_id — o carimbo de origem
  é server-side em routes/pets.py). Mesmo trade-off do precedente 0092 (users).

PERFORMANCE:
  Reusa o índice composto ix_tenant_tutor_access_lookup (tutor_user_id, tenant_id,
  status) criado na 0092 e o índice existente em pets.tutor_id (modelo). Nenhum índice
  novo é necessário.

PG-only; NO-OP em sqlite. Idempotente (DROP POLICY IF EXISTS + CREATE POLICY).
Downgrade restaura EXATAMENTE a policy padrão da 0044 (escopo + NULL) nessas tabelas.

Revision ID: 0093_rls_pets_follow_tutor
Revises: 0092_rls_users_tenant_members
Create Date: 2026-07-03
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0093_rls_pets_follow_tutor"
down_revision: Union[str, None] = "0092_rls_users_tenant_members"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_POLICY = "tenant_isolation"

# Ramos padrão da casa (0044): escopo global '*' OU tenant_id NULL OU tenant do escopo.
_BASE = (
    "current_setting('app.current_tenant', true) = '*' "
    "OR tenant_id IS NULL "
    "OR tenant_id::text = current_setting('app.current_tenant', true)"
)

# ── pets: base + ramo DONO + ramo VÍNCULO ATIVO ─────────────────────────────
_PETS_PREDICATE = (
    _BASE
    + " OR ("
    "current_setting('app.current_user_id', true) NOT IN ('-', '') "
    "AND tutor_id::text = current_setting('app.current_user_id', true)"
    ") "
    "OR EXISTS ("
    "SELECT 1 FROM tenant_tutor_access a "
    "WHERE a.tutor_user_id = pets.tutor_id "
    "AND a.tenant_id = current_setting('app.current_tenant', true) "
    "AND a.status = 'active'"
    ")"
)

# Eventos de origem operacional do tenant que NÃO seguem o tutor.
_OPERATIONAL_EVENT_TYPES = "('walk_observation', 'tenant_note')"


def _satellite_predicate(table: str, *, event_type_guard: bool = False) -> str:
    """Predicado do satélite: base padrão + ramo DONO e ramo VÍNCULO via JOIN com pets.

    Para pet_timeline_events (event_type_guard=True) os ramos novos ficam restritos
    aos eventos que SEGUEM o tutor (event_type NOT IN operacionais).
    """
    guard = (
        f" AND {table}.event_type NOT IN {_OPERATIONAL_EVENT_TYPES}"
        if event_type_guard
        else ""
    )
    return (
        _BASE
        + " OR EXISTS ("
        "SELECT 1 FROM pets p "
        f"WHERE p.id = {table}.pet_id "
        "AND current_setting('app.current_user_id', true) NOT IN ('-', '') "
        "AND p.tutor_id::text = current_setting('app.current_user_id', true)"
        f"{guard}"
        ") "
        "OR EXISTS ("
        "SELECT 1 FROM pets p "
        "JOIN tenant_tutor_access a ON a.tutor_user_id = p.tutor_id "
        f"WHERE p.id = {table}.pet_id "
        "AND a.tenant_id = current_setting('app.current_tenant', true) "
        "AND a.status = 'active'"
        f"{guard}"
        ")"
    )


# Tabelas satélite de SAÚDE que seguem o tutor (sem guard de event_type).
_HEALTH_SATELLITES = (
    "pet_health_records",
    "pet_reminders",
    "pet_share_links",
    "pet_self_walks",
)

# Todas as tabelas tocadas, com o predicado a aplicar.
def _table_predicates() -> dict[str, str]:
    preds = {"pets": _PETS_PREDICATE}
    for t in _HEALTH_SATELLITES:
        preds[t] = _satellite_predicate(t)
    preds["pet_timeline_events"] = _satellite_predicate(
        "pet_timeline_events", event_type_guard=True
    )
    return preds


def _recreate(table: str, predicate: str) -> None:
    op.execute(f'DROP POLICY IF EXISTS {_POLICY} ON "{table}"')
    op.execute(
        f'CREATE POLICY {_POLICY} ON "{table}" '
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        # NO-OP em SQLite (CI/testes) — policies são feature exclusiva do PostgreSQL.
        return
    for table, predicate in _table_predicates().items():
        _recreate(table, predicate)


def downgrade() -> None:
    # Restaura EXATAMENTE a policy padrão da 0044 (escopo + NULL) nessas tabelas.
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    for table in _table_predicates():
        _recreate(table, _BASE)
