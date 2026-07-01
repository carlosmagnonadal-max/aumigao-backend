"""walk_observations: observação estruturada do passeador por passeio (Fase 2)

Revision ID: 0074_walk_observations
Revises: 0073_pet_live_profile
"""
from alembic import op
import sqlalchemy as sa

revision = "0074_walk_observations"
down_revision = "0073_pet_live_profile"
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
        "walk_observations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("walk_id", sa.String(), sa.ForeignKey("walks.id"), nullable=False),
        sa.Column("pet_id", sa.String(), sa.ForeignKey("pets.id"), nullable=False),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("walker_user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("mood", sa.String(), nullable=True),
        sa.Column("energy", sa.String(), nullable=True),
        sa.Column("socialization", sa.String(), nullable=True),
        sa.Column("peed", sa.Boolean(), nullable=True),
        sa.Column("pooped", sa.Boolean(), nullable=True),
        sa.Column("incident", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("incident_notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_walk_observations_walk_id", "walk_observations", ["walk_id"], unique=True)
    op.create_index("ix_walk_observations_pet_id", "walk_observations", ["pet_id"])
    op.create_index("ix_walk_observations_tenant_id", "walk_observations", ["tenant_id"])

    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        _enable_rls(conn, "walk_observations")


def downgrade() -> None:
    op.drop_table("walk_observations")
