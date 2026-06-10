"""Onda 1 — planos recorrentes: recurring_plans + tutor_subscriptions

Aditivo e reversível: catálogo de planos recorrentes por tenant e assinaturas
dos tutores. Gated pela feature flag `recurring_plans` (tenant_features).

Revision ID: 0011_recurring_plans
Revises: 0010_payment_split
Create Date: 2026-06-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0011_recurring_plans"
# Encadeado após 0011_upload_files (que também descende de 0010) para manter a
# árvore de migrations LINEAR — evita duas heads e quebra de deploy.
down_revision: Union[str, None] = "0011_upload_files"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "recurring_plans",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("price", sa.Float(), nullable=False, server_default="0"),
        sa.Column("walks_per_cycle", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("interval", sa.String(), nullable=False, server_default="monthly"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_recurring_plans_tenant_id", "recurring_plans", ["tenant_id"])

    op.create_table(
        "tutor_subscriptions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("plan_id", sa.String(), nullable=False),
        sa.Column("tutor_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("price", sa.Float(), nullable=False, server_default="0"),
        sa.Column("walks_per_cycle", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("credits_remaining", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_period_start", sa.DateTime(), nullable=True),
        sa.Column("current_period_end", sa.DateTime(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["plan_id"], ["recurring_plans.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tutor_subscriptions_tenant_id", "tutor_subscriptions", ["tenant_id"])
    op.create_index("ix_tutor_subscriptions_plan_id", "tutor_subscriptions", ["plan_id"])
    op.create_index("ix_tutor_subscriptions_tutor_id", "tutor_subscriptions", ["tutor_id"])


def downgrade() -> None:
    op.drop_index("ix_tutor_subscriptions_tutor_id", table_name="tutor_subscriptions")
    op.drop_index("ix_tutor_subscriptions_plan_id", table_name="tutor_subscriptions")
    op.drop_index("ix_tutor_subscriptions_tenant_id", table_name="tutor_subscriptions")
    op.drop_table("tutor_subscriptions")
    op.drop_index("ix_recurring_plans_tenant_id", table_name="recurring_plans")
    op.drop_table("recurring_plans")
