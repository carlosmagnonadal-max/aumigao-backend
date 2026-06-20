"""Fase 3 — RLS: habilita Row-Level Security + policy tenant_isolation.

ESTADO INICIAL: inerte (fail-open para o owner).
A policy existe e está ativa, mas o papel que conecta hoje
(DATABASE_URL) tem BYPASSRLS implícito por ser superuser/owner.
A policy só começa a restringir quando o DATABASE_URL for trocado
para um role sem BYPASSRLS (ex.: role "app") — isso é o "cutover".
Enquanto o cutover não acontecer, o comportamento de produção é
idêntico ao atual (sem RLS), e a app pode ser deployada com segurança.

POLICY:
  USING (
    current_setting('app.current_tenant', true) = '*'
    OR tenant_id::text = current_setting('app.current_tenant', true)
  )
  WITH CHECK (
    current_setting('app.current_tenant', true) = '*'
    OR tenant_id::text = current_setting('app.current_tenant', true)
  )

Sentinela '*' → super_admin / callers internos globais (veem tudo).
GUC ausente / vazio → fail-closed (0 linhas visíveis).
FORCE ROW LEVEL SECURITY NÃO é habilitado aqui: o owner deve
continuar vendo tudo durante a fase inerte.

Revision ID: 0043_enable_rls
Revises: 0042_backfill_encrypt_cpf_rg
Create Date: 2026-06-20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0043_enable_rls"
down_revision: Union[str, None] = "0042_backfill_encrypt_cpf_rg"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Tabelas que possuem coluna tenant_id e devem receber a policy de isolamento.
# Lista derivada do SQLAlchemy metadata do projeto (sorted_tables com tenant_id).
# Atualizar esta lista se novas tabelas com tenant_id forem criadas.
_TENANT_TABLES: Sequence[str] = [
    "app_settings",
    "audit_logs",
    "complaints",
    "contact_messages",
    "coupon_redemptions",
    "coupons",
    "incentive_rules",
    "notifications",
    "payments",
    "pets",
    "recurring_plans",
    "shared_walks",
    "support_tickets",
    "tenant_branding",
    "tenant_features",
    "tenant_individual_walk_pricing",
    "tenant_onboarding",
    "tenant_payment_configs",
    "tenant_pet_tour_configs",
    "tenant_settings",
    "tenant_shared_walk_configs",
    "tenant_units",
    "tenant_walker_access",
    "tutor_profiles",
    "tutor_subscriptions",
    "upload_files",
    "user_role_assignments",
    "users",
    "walk_completion_reviews",
    "walk_reviews",
    "walk_tips",
    "walker_reviews",
    "walks",
]

_POLICY_USING = (
    "current_setting('app.current_tenant', true) = '*' "
    "OR tenant_id::text = current_setting('app.current_tenant', true)"
)


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        # NO-OP em SQLite (CI/testes) — policies são feature exclusiva do PostgreSQL.
        return

    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())

    for table in _TENANT_TABLES:
        if table not in existing_tables:
            # Tabela pode não existir em ambientes muito antigos; pula silenciosamente.
            continue

        # Verifica que a coluna tenant_id realmente existe (segurança extra).
        col_names = {c["name"] for c in inspector.get_columns(table)}
        if "tenant_id" not in col_names:
            continue

        conn.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        conn.execute(sa.text(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"'))
        conn.execute(sa.text(
            f'CREATE POLICY tenant_isolation ON "{table}" '
            f"USING ({_POLICY_USING}) "
            f"WITH CHECK ({_POLICY_USING})"
        ))


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())

    for table in _TENANT_TABLES:
        if table not in existing_tables:
            continue

        conn.execute(sa.text(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"'))
        conn.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))
