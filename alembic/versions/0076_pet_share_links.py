"""pet_share_links: tokens públicos LGPD para compartilhar perfil do pet (Fase 4)

Revision ID: 0076_pet_share_links
Revises: 0075_pet_reminders
"""
from alembic import op
import sqlalchemy as sa

revision = "0076_pet_share_links"
down_revision = "0075_pet_reminders"
branch_labels = None
depends_on = None

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
        "pet_share_links",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("token", sa.String(), nullable=False, unique=True),
        sa.Column("pet_id", sa.String(), sa.ForeignKey("pets.id"), nullable=False),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("created_by", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("consent_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_pet_share_links_token", "pet_share_links", ["token"], unique=True)
    op.create_index("ix_pet_share_links_pet_id", "pet_share_links", ["pet_id"])
    op.create_index("ix_pet_share_links_tenant_id", "pet_share_links", ["tenant_id"])

    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        _enable_rls(conn, "pet_share_links")


def downgrade() -> None:
    op.drop_table("pet_share_links")
