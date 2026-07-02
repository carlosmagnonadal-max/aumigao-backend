"""pet_health_records + ficha rica do pet (Perfil Vivo 2.0 — Fase A)

Adiciona:
  1. Colunas aditivas NULL em `pets`: microchip + dieta estruturada (tipo, marca,
     linha, gramas/refeição, refeições/dia, horários, observações). vet_name/vet_phone
     JÁ existem desde a 0073 (não re-adicionar). O campo `microchip` é distinto do
     `chip_number` (0073): chip_number = número do chip legado; microchip = campo
     canônico da ficha rica 2.0 (mantidos separados para não sobrescrever dados).
  2. Tabela nova `pet_health_records` — carteira de saúde (vacina/vermífugo/antipulgas/
     tratamento) com RLS no padrão EXATO das 0073-0077 (tenant + NULL allowance).

Aditiva (colunas NULL, tabela nova) — zero impacto em dados existentes.

Revision ID: 0086_pet_health_records
Revises: 0085_tenant_free_plan_trial
"""
from alembic import op
import sqlalchemy as sa

revision = "0086_pet_health_records"
down_revision = "0085_tenant_free_plan_trial"
branch_labels = None
depends_on = None

# Padrão da casa (0073-0077): tenant-scoped USING/WITH CHECK com NULL allowance.
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
    # 1. Ficha rica em pets (aditivo, NULL) — microchip + dieta estruturada.
    op.add_column("pets", sa.Column("microchip", sa.String(), nullable=True))
    op.add_column("pets", sa.Column("diet_type", sa.String(), nullable=True))
    op.add_column("pets", sa.Column("diet_brand", sa.String(), nullable=True))
    op.add_column("pets", sa.Column("diet_line", sa.String(), nullable=True))
    op.add_column("pets", sa.Column("diet_grams_per_meal", sa.Integer(), nullable=True))
    op.add_column("pets", sa.Column("diet_meals_per_day", sa.Integer(), nullable=True))
    op.add_column("pets", sa.Column("diet_meal_times", sa.String(), nullable=True))
    op.add_column("pets", sa.Column("diet_notes", sa.Text(), nullable=True))

    # 2. Carteira de saúde — uma tabela para tudo (vacina/vermífugo/antipulgas/tratamento).
    op.create_table(
        "pet_health_records",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("pet_id", sa.String(), sa.ForeignKey("pets.id"), nullable=False),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),  # vaccine|dewormer|flea_tick|treatment
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("applied_at", sa.Date(), nullable=False),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by_role", sa.String(), nullable=False, server_default="tutor"),  # tutor|admin
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_pet_health_records_pet_id", "pet_health_records", ["pet_id"])
    op.create_index("ix_pet_health_records_tenant_id", "pet_health_records", ["tenant_id"])
    op.create_index("ix_pet_health_records_kind", "pet_health_records", ["kind"])
    op.create_index("ix_pet_health_records_pet_kind", "pet_health_records", ["pet_id", "kind"])

    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        _enable_rls(conn, "pet_health_records")


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text('DROP POLICY IF EXISTS tenant_isolation ON "pet_health_records"'))
    op.drop_table("pet_health_records")
    for col in (
        "diet_notes", "diet_meal_times", "diet_meals_per_day", "diet_grams_per_meal",
        "diet_line", "diet_brand", "diet_type", "microchip",
    ):
        op.drop_column("pets", col)
