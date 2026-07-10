"""Alertas de custo: cost_alerts + cost_alert_events (RLS-ON).

Revision ID: 0106_cost_alerts
Revises: 0105_walks_security_code
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0106_cost_alerts"
down_revision: Union[str, None] = "0105_walks_security_code"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Mesma policy da 0043 (padrão do projeto)
_POLICY_USING = (
    "current_setting('app.current_tenant', true) = '*' "
    "OR tenant_id::text = current_setting('app.current_tenant', true)"
)
_TABLES = ("cost_alerts", "cost_alert_events")


def upgrade() -> None:
    op.create_table(
        "cost_alerts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False, index=True),
        sa.Column("owner_type", sa.String(), nullable=False, server_default="tenant"),
        sa.Column("owner_user_id", sa.String(), nullable=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("scope", sa.String(), nullable=False, server_default="total"),
        sa.Column("currency", sa.String(), nullable=False, server_default="BRL"),
        sa.Column("budget_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("period", sa.String(), nullable=False, server_default="monthly"),
        sa.Column("thresholds_json", sa.String(), nullable=False, server_default="[50, 80, 100]"),
        sa.Column("evaluation", sa.String(), nullable=False, server_default="both"),
        sa.Column("channels_json", sa.String(), nullable=False, server_default='["in_app"]'),
        sa.Column("status", sa.String(), nullable=False, server_default="active", index=True),
        sa.Column("config_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "cost_alert_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False, index=True),
        sa.Column("alert_id", sa.String(), nullable=False, index=True),
        sa.Column("period_key", sa.String(), nullable=False),
        sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("spend_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("budget_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("channels_json", sa.String(), nullable=False, server_default="[]"),
        sa.Column("delivery_json", sa.String(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("alert_id", "period_key", "threshold", "kind", "config_version",
                            name="uq_cost_alert_events_dedupe"),
    )
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        for table in _TABLES:
            conn.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
            conn.execute(sa.text(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"'))
            conn.execute(sa.text(
                f'CREATE POLICY tenant_isolation ON "{table}" '
                f"USING ({_POLICY_USING}) WITH CHECK ({_POLICY_USING})"
            ))


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        for table in _TABLES:
            conn.execute(sa.text(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"'))
    op.drop_table("cost_alert_events")
    op.drop_table("cost_alerts")
