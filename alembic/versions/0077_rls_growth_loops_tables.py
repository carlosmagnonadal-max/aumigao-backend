"""rls_growth_loops_tables: habilita RLS nas 3 tabelas dos growth loops (P0-1)

As tabelas walk_share_links (0069), tutor_referral_configs e tutor_referrals
(0070) foram criadas SEM ROW LEVEL SECURITY — ficaram fora do padrao da casa
(_enable_rls das migrations 0073-0076). Confirmado em prod: rls=False, policies=0.

Esta migration replica EXATAMENTE o mesmo padrao tenant-scoped USING/WITH CHECK
com NULL allowance das 0073-0076. A NULL allowance e critica: essas tabelas TEM
dados em prod (o Aumigao ativou referral) e ha inserts legitimos com tenant_id
NULL / escopo global (rota publica de convite) que nao podem quebrar.

Revision ID: 0077_rls_growth_loops_tables
Revises: 0076_pet_share_links
"""
from alembic import op
import sqlalchemy as sa

revision = "0077_rls_growth_loops_tables"
down_revision = "0076_pet_share_links"
branch_labels = None
depends_on = None

_TABLES = ("walk_share_links", "tutor_referral_configs", "tutor_referrals")

_POLICY_USING = (
    "current_setting('app.current_tenant', true) = '*' "
    "OR tenant_id IS NULL "
    "OR tenant_id::text = current_setting('app.current_tenant', true)"
)


def _enable_rls(conn, table: str) -> None:
    conn.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
    conn.execute(sa.text(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"'))
    conn.execute(sa.text(
        f'CREATE POLICY tenant_isolation ON "{table}" '
        f"USING ({_POLICY_USING}) "
        f"WITH CHECK ({_POLICY_USING})"
    ))


def _disable_rls(conn, table: str) -> None:
    conn.execute(sa.text(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"'))
    conn.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        for table in _TABLES:
            _enable_rls(conn, table)


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        for table in _TABLES:
            _disable_rls(conn, table)
