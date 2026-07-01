"""pet_live_profile: campos longitudinais do pet + timeline + config (Fase 1)

Revision ID: 0073_pet_live_profile
Revises: 0072_tutor_referral_held_credits
"""
from alembic import op
import sqlalchemy as sa

revision = "0073_pet_live_profile"
down_revision = "0072_tutor_referral_held_credits"
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
    op.add_column("pets", sa.Column("birth_date", sa.Date(), nullable=True))
    op.add_column("pets", sa.Column("chip_number", sa.String(), nullable=True))
    op.add_column("pets", sa.Column("vet_name", sa.String(), nullable=True))
    op.add_column("pets", sa.Column("vet_phone", sa.String(), nullable=True))
    op.add_column("pets", sa.Column("emergency_contact", sa.String(), nullable=True))

    op.create_table(
        "pet_timeline_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("pet_id", sa.String(), sa.ForeignKey("pets.id"), nullable=False),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("source", sa.String(), nullable=False, server_default="tutor"),
        sa.Column("created_by_user_id", sa.String(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("related_entity_type", sa.String(), nullable=True),
        sa.Column("related_entity_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_pet_timeline_events_pet_id", "pet_timeline_events", ["pet_id"])
    op.create_index("ix_pet_timeline_events_tenant_id", "pet_timeline_events", ["tenant_id"])
    op.create_index("ix_pet_timeline_events_event_type", "pet_timeline_events", ["event_type"])
    op.create_index("ix_pet_timeline_events_occurred_at", "pet_timeline_events", ["occurred_at"])
    op.create_index("ix_pet_timeline_events_pet_occurred", "pet_timeline_events", ["pet_id", "occurred_at"])

    op.create_table(
        "pet_profile_configs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("profile_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("observations_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reminders_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("vaccine_lead_days", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("inactivity_days", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("share_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_pet_profile_configs_tenant_id", "pet_profile_configs", ["tenant_id"], unique=True)

    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        _enable_rls(conn, "pet_timeline_events")
        _enable_rls(conn, "pet_profile_configs")


def downgrade() -> None:
    op.drop_table("pet_profile_configs")
    op.drop_table("pet_timeline_events")
    for col in ("emergency_contact", "vet_phone", "vet_name", "chip_number", "birth_date"):
        op.drop_column("pets", col)
