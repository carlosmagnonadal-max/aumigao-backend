"""legal_acceptance_v2 — aceite legal em 2 camadas (plataforma + tenant).

(a) Formaliza a tabela `legal_acceptances` que existe em PROD (16 linhas), criada
    por um create_all antigo e NUNCA registrada numa migration. CREATE TABLE IF NOT
    EXISTS com as MESMAS colunas do modelo + nova coluna `tenant_id` (String, nullable,
    index): NULL = aceite de PLATAFORMA; preenchido = aceite POR TENANT (Modelo B, a
    relacao do passeio e com o estabelecimento).
(b) Tabela nova `tenant_legal_documents`: documentos legais que o tenant configura no
    admin a partir de MODELOS BASE (Fase 2). Versionamento simples por is_active: no
    maximo UMA versao ativa por (tenant_id, doc_type); versoes antigas ficam inativas
    para historico/auditoria.
(c) RLS no padrao EXATO da casa (0043-0045 / 0073-0077 / 0090): tenant-scoped USING/
    WITH CHECK com NULL allowance. IMPORTANTE: `legal_acceptances` NUNCA teve policy
    RLS (nao tinha coluna tenant_id quando a 0043 rodou -> ficou fora da introspeccao).
    Aplicamos a policy padrao AGORA. O ramo `tenant_id IS NULL` cobre os aceites de
    PLATAFORMA (tenant_id NULL) lidos sob escopo de um tenant — sem isso, a leitura do
    status de plataforma quebraria sob a GUC de um tenant especifico.

Aditiva/idempotente — zero impacto destrutivo nos dados existentes.

Revision ID: 0096_legal_acceptance_v2
Revises: 0095_highlight_product_url
Create Date: 2026-07-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0096_legal_acceptance_v2"
down_revision: Union[str, None] = "0095_highlight_product_url"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Padrao da casa (0043-0045 / 0073-0077 / 0090): tenant-scoped com NULL allowance.
_POLICY_USING = (
    "current_setting('app.current_tenant', true) = '*' "
    "OR tenant_id IS NULL "
    "OR tenant_id::text = current_setting('app.current_tenant', true)"
)


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


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
    # ---- (a) formaliza legal_acceptances (existe em prod via create_all antigo) ----
    if not _has_table("legal_acceptances"):
        op.create_table(
            "legal_acceptances",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("user_role", sa.String(), nullable=False),
            sa.Column("terms_version", sa.String(), nullable=False, server_default=""),
            sa.Column("privacy_version", sa.String(), nullable=False, server_default=""),
            sa.Column("cancellation_version", sa.String(), nullable=False, server_default=""),
            sa.Column("lgpd_version", sa.String(), nullable=False, server_default=""),
            sa.Column("geolocation_version", sa.String(), nullable=False, server_default=""),
            sa.Column("accepted_at", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_legal_acceptances_user_id", "legal_acceptances", ["user_id"])
        op.create_index("ix_legal_acceptances_user_role", "legal_acceptances", ["user_role"])
        op.create_index("ix_legal_acceptances_accepted_at", "legal_acceptances", ["accepted_at"])

    # tenant_id: NULL = plataforma; preenchido = aceite por tenant.
    if not _has_column("legal_acceptances", "tenant_id"):
        op.add_column("legal_acceptances", sa.Column("tenant_id", sa.String(), nullable=True))
        op.create_index(
            "ix_legal_acceptances_tenant_id", "legal_acceptances", ["tenant_id"]
        )

    # ---- (b) tabela nova tenant_legal_documents ----
    if not _has_table("tenant_legal_documents"):
        op.create_table(
            "tenant_legal_documents",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("doc_type", sa.String(length=64), nullable=False),
            sa.Column("title", sa.String(length=200), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("version", sa.String(length=64), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_tenant_legal_documents_tenant_id", "tenant_legal_documents", ["tenant_id"]
        )
        # Leitura vigente: doc ATIVO por (tenant, doc_type).
        op.create_index(
            "ix_tenant_legal_documents_tenant_type_active",
            "tenant_legal_documents",
            ["tenant_id", "doc_type", "is_active"],
        )

    # ---- (c) RLS padrao nas duas tabelas ----
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        _enable_rls(conn, "legal_acceptances")
        _enable_rls(conn, "tenant_legal_documents")


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        _disable_rls(conn, "tenant_legal_documents")
        # legal_acceptances existia ANTES desta migration (sem RLS): remove so a policy.
        _disable_rls(conn, "legal_acceptances")

    if _has_table("tenant_legal_documents"):
        op.drop_index(
            "ix_tenant_legal_documents_tenant_type_active",
            table_name="tenant_legal_documents",
        )
        op.drop_index(
            "ix_tenant_legal_documents_tenant_id", table_name="tenant_legal_documents"
        )
        op.drop_table("tenant_legal_documents")

    # Nao removemos legal_acceptances (existia antes). So a coluna/indice adicionados.
    if _has_column("legal_acceptances", "tenant_id"):
        op.drop_index("ix_legal_acceptances_tenant_id", table_name="legal_acceptances")
        op.drop_column("legal_acceptances", "tenant_id")
