"""0098 — walker_indications + walker_leads

Tabelas:
  - walker_indications: indicação de passeador feita por tutor autenticado.
  - walker_leads:       lead público (página /seja-passeador).

RLS no padrão da casa (0090): tenant-scoped USING/WITH CHECK com NULL allowance.
PG-only; NO-OP em SQLite (CI/testes).

walker_indications: tutor indica um passeador conhecido.
  Ramo extra do tutor dono: tutor_user_id == app.current_user_id (padrão das
  tabelas de tutor — espelhado de pet_profile_configs/pet_share_links/0094).

walker_leads: gerado via rota pública (/api/public/walker-leads) ou pelo sistema.
  Apenas isolamento por tenant (padrão 0090).

Revision ID: 0098_walker_indications
Revises: 0097_tenant_units_slug_rbac
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0098_walker_indications"
down_revision: Union[str, None] = "0097_tenant_units_slug_rbac"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Padrão da casa (0090): base tenant-scoped com NULL allowance.
_BASE = (
    "current_setting('app.current_tenant', true) = '*' "
    "OR tenant_id IS NULL "
    "OR tenant_id::text = current_setting('app.current_tenant', true)"
)

# walker_indications: base + ramo do tutor dono (espelha pet_profile_configs).
_INDICATIONS_PREDICATE = (
    _BASE
    + " OR ("
    "current_setting('app.current_user_id', true) NOT IN ('-', '') "
    "AND tutor_user_id::text = current_setting('app.current_user_id', true)"
    ")"
)


def _enable_rls(conn, table: str, predicate: str) -> None:
    conn.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
    conn.execute(sa.text(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"'))
    conn.execute(
        sa.text(
            f'CREATE POLICY tenant_isolation ON "{table}" '
            f"USING ({predicate}) "
            f"WITH CHECK ({predicate})"
        )
    )


def upgrade() -> None:
    op.create_table(
        "walker_indications",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "tutor_user_id",
            sa.String(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("walker_name", sa.String(200), nullable=False),
        sa.Column("walker_phone", sa.String(30), nullable=True),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column(
            "status",
            sa.String(30),
            nullable=False,
            server_default="enviada",
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_walker_indications_tenant_id", "walker_indications", ["tenant_id"]
    )
    op.create_index(
        "ix_walker_indications_tutor_user_id",
        "walker_indications",
        ["tutor_user_id"],
    )
    op.create_index(
        "ix_walker_indications_status", "walker_indications", ["status"]
    )

    op.create_table(
        "walker_leads",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("phone", sa.String(30), nullable=False),
        sa.Column("city", sa.String(120), nullable=True),
        sa.Column(
            "indication_id",
            sa.String(),
            sa.ForeignKey("walker_indications.id"),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.String(30),
            nullable=False,
            server_default="novo",
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_walker_leads_tenant_id", "walker_leads", ["tenant_id"]
    )
    op.create_index(
        "ix_walker_leads_indication_id", "walker_leads", ["indication_id"]
    )
    op.create_index("ix_walker_leads_status", "walker_leads", ["status"])

    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        _enable_rls(conn, "walker_indications", _INDICATIONS_PREDICATE)
        _enable_rls(conn, "walker_leads", _BASE)


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.execute(
            sa.text('DROP POLICY IF EXISTS tenant_isolation ON "walker_leads"')
        )
        conn.execute(
            sa.text(
                'ALTER TABLE "walker_leads" DISABLE ROW LEVEL SECURITY'
            )
        )
        conn.execute(
            sa.text(
                'DROP POLICY IF EXISTS tenant_isolation ON "walker_indications"'
            )
        )
        conn.execute(
            sa.text(
                'ALTER TABLE "walker_indications" DISABLE ROW LEVEL SECURITY'
            )
        )

    op.drop_index("ix_walker_leads_status", table_name="walker_leads")
    op.drop_index(
        "ix_walker_leads_indication_id", table_name="walker_leads"
    )
    op.drop_index("ix_walker_leads_tenant_id", table_name="walker_leads")
    op.drop_table("walker_leads")

    op.drop_index(
        "ix_walker_indications_status", table_name="walker_indications"
    )
    op.drop_index(
        "ix_walker_indications_tutor_user_id",
        table_name="walker_indications",
    )
    op.drop_index(
        "ix_walker_indications_tenant_id", table_name="walker_indications"
    )
    op.drop_table("walker_indications")
