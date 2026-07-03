"""pet_self_walks — passeio self-serve do tutor (Perfil Vivo 2.0 — Fase D)

Tabela nova `pet_self_walks`: RESUMO de um passeio que o tutor faz com o próprio
cão (engajamento/dado, NÃO transação — sem comissão, sem passeador). O cliente
rastreia localmente e envia uma vez; o servidor persiste só o resumo (sem rota
GPS — zero purge novo).

Colunas explícitas (needs/behavior como bool) em vez de JSON: escolha pensando na
agregação futura do wellness (componente Rotina já soma self-walks; evolução
natural = filtrar/contar por comportamento). Ver docstring do modelo.

RLS no padrão EXATO das 0073-0086 (tenant-scoped USING/WITH CHECK com NULL
allowance) — a introspecção do conftest de RLS aplica a policy padrão
automaticamente (tabela tem coluna tenant_id).

Aditiva (tabela nova) — zero impacto em dados existentes.

Revision ID: 0087_pet_self_walks
Revises: 0086_pet_health_records
"""
from alembic import op
import sqlalchemy as sa

revision = "0087_pet_self_walks"
down_revision = "0086_pet_health_records"
branch_labels = None
depends_on = None

# Padrão da casa (0073-0086): tenant-scoped USING/WITH CHECK com NULL allowance.
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


def upgrade() -> None:
    op.create_table(
        "pet_self_walks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("pet_id", sa.String(), sa.ForeignKey("pets.id"), nullable=False),
        sa.Column("tutor_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("distance_km", sa.Numeric(6, 2), nullable=True),
        sa.Column("walk_type", sa.String(), nullable=False),  # rua|parque|praia|trilha|interno|outro
        sa.Column("intensity", sa.String(), nullable=False),  # leve|moderado|intenso
        sa.Column("had_gps", sa.Boolean(), nullable=False, server_default=sa.false()),
        # needs (necessidades)
        sa.Column("need_pee", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("need_poop", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("need_water", sa.Boolean(), nullable=False, server_default=sa.false()),
        # behavior (comportamento)
        sa.Column("interacted_dogs", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("interacted_people", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pulled_leash", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("showed_fear", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("showed_reactivity", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_pet_self_walks_pet_id", "pet_self_walks", ["pet_id"])
    op.create_index("ix_pet_self_walks_tutor_id", "pet_self_walks", ["tutor_id"])
    op.create_index("ix_pet_self_walks_tenant_id", "pet_self_walks", ["tenant_id"])
    op.create_index("ix_pet_self_walks_started_at", "pet_self_walks", ["started_at"])
    op.create_index("ix_pet_self_walks_pet_started", "pet_self_walks", ["pet_id", "started_at"])

    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        _enable_rls(conn, "pet_self_walks")


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text('DROP POLICY IF EXISTS tenant_isolation ON "pet_self_walks"'))
    op.drop_table("pet_self_walks")
