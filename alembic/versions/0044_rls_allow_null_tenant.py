"""RLS: permite tenant_id NULL nas policies (migration 0044).

A policy da 0043 bloqueava INSERT/SELECT de linhas com tenant_id NULL — mas vários
fluxos legítimos criam linhas SEM tenant (ex.: upload de documentos de CANDIDATURA
de passeador, que é anônima/global, grava upload_files com tenant_id=None). Sob a
policy estrita isso dava "new row violates row-level security policy".

Correção: adiciona `tenant_id IS NULL` ao USING e ao WITH CHECK. Linhas sem tenant
são anônimas/globais (não pertencem a nenhum petshop) → liberá-las NÃO vaza dado
privado entre tenants (as tabelas de dados privados — tutor_profiles, walks, pets,
payments — têm 0 linhas com tenant_id NULL, validado em 2026-06-20).

PG-only; NO-OP em sqlite. Idempotente (DROP POLICY IF EXISTS + CREATE).

Revision ID: 0044_rls_allow_null_tenant
Revises: 0043_enable_rls
Create Date: 2026-06-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0044_rls_allow_null_tenant"
down_revision: Union[str, None] = "0043_enable_rls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_POLICY = "tenant_isolation"
_PREDICATE = (
    "current_setting('app.current_tenant', true) = '*' "
    "OR tenant_id IS NULL "
    "OR tenant_id::text = current_setting('app.current_tenant', true)"
)


def _tenant_tables(conn) -> list[str]:
    insp = sa.inspect(conn)
    out = []
    for t in insp.get_table_names():
        if any(c["name"] == "tenant_id" for c in insp.get_columns(t)):
            out.append(t)
    return out


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    for t in _tenant_tables(conn):
        op.execute(f'DROP POLICY IF EXISTS {_POLICY} ON "{t}"')
        op.execute(
            f'CREATE POLICY {_POLICY} ON "{t}" '
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    # Volta para a policy estrita (sem permitir NULL) — comportamento da 0043.
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    strict = (
        "current_setting('app.current_tenant', true) = '*' "
        "OR tenant_id::text = current_setting('app.current_tenant', true)"
    )
    for t in _tenant_tables(conn):
        op.execute(f'DROP POLICY IF EXISTS {_POLICY} ON "{t}"')
        op.execute(
            f'CREATE POLICY {_POLICY} ON "{t}" '
            f"USING ({strict}) WITH CHECK ({strict})"
        )
