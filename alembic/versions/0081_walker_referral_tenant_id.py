"""walker_referral_tenant_id: adiciona tenant_id em walker_referrals + habilita RLS.

A tabela walker_referrals (cunha ③ referral do passeador, gated OFF) foi criada
via Base.metadata.create_all no startup, SEM migration e SEM RLS. Sem tenant_id,
o WalkerEarning de referral ficava com tenant_id=None, quebrando o isolamento
multi-tenant ao ligar WALKER_REFERRAL_PAYOUT_ENABLED.

Esta migration:
  - adiciona a coluna tenant_id (FK tenants, nullable p/ linhas legadas) — só se a
    tabela existir e a coluna ainda não existir (idempotente/defensivo);
  - habilita RLS no padrão tenant-scoped da casa (USING/WITH CHECK com allowance de
    '*' e NULL), igual às migrations 0073-0077.

Revision ID: 0081_walker_referral_tenant_id
Revises: 0080_webhook_events
"""
from alembic import op
import sqlalchemy as sa

revision = "0081_walker_referral_tenant_id"
down_revision = "0080_webhook_events"
branch_labels = None
depends_on = None

_TABLE = "walker_referrals"

_POLICY_USING = (
    "current_setting('app.current_tenant', true) = '*' "
    "OR tenant_id IS NULL "
    "OR tenant_id::text = current_setting('app.current_tenant', true)"
)


def _table_exists(conn, table: str) -> bool:
    return sa.inspect(conn).has_table(table)


def _column_exists(conn, table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(conn).get_columns(table))


def upgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, _TABLE):
        return

    if not _column_exists(conn, _TABLE, "tenant_id"):
        op.add_column(_TABLE, sa.Column("tenant_id", sa.String(), nullable=True))
        op.create_index(f"ix_{_TABLE}_tenant_id", _TABLE, ["tenant_id"])
        if conn.dialect.name == "postgresql":
            op.create_foreign_key(
                f"fk_{_TABLE}_tenant_id", _TABLE, "tenants", ["tenant_id"], ["id"]
            )

    if conn.dialect.name == "postgresql":
        conn.execute(sa.text(f'ALTER TABLE "{_TABLE}" ENABLE ROW LEVEL SECURITY'))
        conn.execute(sa.text(f'DROP POLICY IF EXISTS tenant_isolation ON "{_TABLE}"'))
        conn.execute(sa.text(
            f'CREATE POLICY tenant_isolation ON "{_TABLE}" '
            f"USING ({_POLICY_USING}) WITH CHECK ({_POLICY_USING})"
        ))


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, _TABLE):
        return
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text(f'DROP POLICY IF EXISTS tenant_isolation ON "{_TABLE}"'))
        conn.execute(sa.text(f'ALTER TABLE "{_TABLE}" DISABLE ROW LEVEL SECURITY'))
        try:
            op.drop_constraint(f"fk_{_TABLE}_tenant_id", _TABLE, type_="foreignkey")
        except Exception:
            pass
    if _column_exists(conn, _TABLE, "tenant_id"):
        try:
            op.drop_index(f"ix_{_TABLE}_tenant_id", table_name=_TABLE)
        except Exception:
            pass
        op.drop_column(_TABLE, "tenant_id")
