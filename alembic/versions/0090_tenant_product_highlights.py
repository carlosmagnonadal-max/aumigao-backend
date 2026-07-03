"""tenant_product_highlights — Vitrine de Destaques e Promoções do tenant (Fase 1)

Tabela nova `tenant_product_highlights`: curadoria de POUCOS produtos/serviços em
destaque/promoção que o tenant mostra no app do tutor (demonstração, sem transação —
Fase 1). NÃO é catálogo/estoque; o limite de itens ATIVOS é imposto no service.

RLS no padrão EXATO das 0073-0077/0086 (tenant + NULL allowance). A tabela recebe a
policy padrão automaticamente na suíte RLS PG (introspecção por coluna tenant_id em
tests/pg_rls/conftest.py) — sem caso especial.

Aditiva (tabela nova) — zero impacto em dados existentes.

Revision ID: 0090_tenant_product_highlights
Revises: 0089_tutor_subscription_cancel_reason
"""
from alembic import op
import sqlalchemy as sa

revision = "0090_tenant_product_highlights"
down_revision = "0089_tutor_subscription_cancel_reason"
branch_labels = None
depends_on = None

# Padrão da casa (0073-0077/0086): tenant-scoped USING/WITH CHECK com NULL allowance.
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
        "tenant_product_highlights",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("photo_url", sa.String(length=2000), nullable=True),
        sa.Column("price_cents", sa.Integer(), nullable=True),
        sa.Column("promo_price_cents", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_tenant_product_highlights_tenant_id", "tenant_product_highlights", ["tenant_id"]
    )
    # Índice para a leitura do app (ativos ordenados por sort_order dentro do tenant).
    op.create_index(
        "ix_tenant_product_highlights_tenant_active",
        "tenant_product_highlights",
        ["tenant_id", "is_active", "sort_order"],
    )

    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        _enable_rls(conn, "tenant_product_highlights")


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.execute(
            sa.text('DROP POLICY IF EXISTS tenant_isolation ON "tenant_product_highlights"')
        )
    op.drop_index(
        "ix_tenant_product_highlights_tenant_active", table_name="tenant_product_highlights"
    )
    op.drop_index(
        "ix_tenant_product_highlights_tenant_id", table_name="tenant_product_highlights"
    )
    op.drop_table("tenant_product_highlights")
