"""0109 — walk_emergency_calls (S1 Botão de Emergência, RLS-ON).

Um registro por acionamento do botão de emergência pelo passeador (cão sob
custódia). tenant_id NULLABLE como walks.tenant_id; policy tenant_isolation com
NULL allowance no USING e WITH CHECK estrito (só '*' grava NULL) — padrão
0045/0106. Idempotente (has_table) — funciona em PG e SQLite.

Spec: docs/superpowers/specs/2026-09-15-seguranca-do-passeio-design.md §1.

Revision ID: 0109_walk_emergency_calls
Revises: 0108_user_apple_sub
Create Date: 2026-09-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0109_walk_emergency_calls"
down_revision: Union[str, None] = "0108_user_apple_sub"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "walk_emergency_calls"
_IX_TENANT = "ix_walk_emergency_calls_tenant_id"
_IX_WALK_CREATED = "ix_walk_emergency_calls_walk_created"

_POLICY_USING = (
    "current_setting('app.current_tenant', true) = '*' "
    "OR tenant_id IS NULL "
    "OR tenant_id::text = current_setting('app.current_tenant', true)"
)
_POLICY_WITH_CHECK = (
    "current_setting('app.current_tenant', true) = '*' "
    "OR tenant_id::text = current_setting('app.current_tenant', true)"
)


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if not _has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("walk_id", sa.String(), sa.ForeignKey("walks.id"), nullable=False),
            sa.Column("walker_user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("tutor_user_id", sa.String(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("reason", sa.String(), nullable=True),
            sa.Column("mode", sa.String(), nullable=False),
            sa.Column("provider", sa.String(), nullable=False, server_default="none"),
            sa.Column("provider_call_id", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("fallback_reason", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("ended_at", sa.DateTime(), nullable=True),
        )
        op.create_index(_IX_TENANT, _TABLE, ["tenant_id"])
        op.create_index(_IX_WALK_CREATED, _TABLE, ["walk_id", "created_at"])

    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text(f'ALTER TABLE "{_TABLE}" ENABLE ROW LEVEL SECURITY'))
        conn.execute(sa.text(f'DROP POLICY IF EXISTS tenant_isolation ON "{_TABLE}"'))
        conn.execute(sa.text(
            f'CREATE POLICY tenant_isolation ON "{_TABLE}" '
            f"USING ({_POLICY_USING}) WITH CHECK ({_POLICY_WITH_CHECK})"
        ))


def downgrade() -> None:
    if not _has_table(_TABLE):
        return
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text(f'DROP POLICY IF EXISTS tenant_isolation ON "{_TABLE}"'))
    op.drop_index(_IX_WALK_CREATED, table_name=_TABLE)
    op.drop_index(_IX_TENANT, table_name=_TABLE)
    op.drop_table(_TABLE)
