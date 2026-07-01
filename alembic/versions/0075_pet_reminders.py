"""pet_reminders: lembretes determinísticos de saúde/atividade do pet (Fase 3)

Revision ID: 0075_pet_reminders
Revises: 0074_walk_observations
"""
from alembic import op
import sqlalchemy as sa

revision = "0075_pet_reminders"
down_revision = "0074_walk_observations"
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
        "pet_reminders",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("pet_id", sa.String(), sa.ForeignKey("pets.id"), nullable=False),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_notified_at", sa.DateTime(), nullable=True),
        sa.Column("source_event_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_pet_reminders_pet_id", "pet_reminders", ["pet_id"])
    op.create_index("ix_pet_reminders_tenant_id", "pet_reminders", ["tenant_id"])
    op.create_index("ix_pet_reminders_kind", "pet_reminders", ["kind"])
    op.create_index("ix_pet_reminders_due_date", "pet_reminders", ["due_date"])

    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        _enable_rls(conn, "pet_reminders")


def downgrade() -> None:
    op.drop_table("pet_reminders")
