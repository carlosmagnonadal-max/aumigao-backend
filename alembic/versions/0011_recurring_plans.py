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


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _create_table(name: str, *args, **kw) -> None:
    # Idempotente: o projeto usa schema-ensure (create_all), que pode ter criado a
    # tabela antes. Só cria se faltar (evita DuplicateTable no upgrade).
    if not _has_table(name):
        op.create_table(name, *args, **kw)


def _create_index(index_name: str, table_name: str, columns, **kw) -> None:
    if not _has_table(table_name):
        return
    existing = {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(table_name)}
    if index_name not in existing:
        op.create_index(index_name, table_name, columns, **kw)


def upgrade() -> None:
    _create_table(
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
    _create_index("ix_recurring_plans_tenant_id", "recurring_plans", ["tenant_id"])

    _create_table(
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
    _create_index("ix_tutor_subscriptions_tenant_id", "tutor_subscriptions", ["tenant_id"])
    _create_index("ix_tutor_subscriptions_plan_id", "tutor_subscriptions", ["plan_id"])
    _create_index("ix_tutor_subscriptions_tutor_id", "tutor_subscriptions", ["tutor_id"])


def downgrade() -> None:
    op.drop_index("ix_tutor_subscriptions_tutor_id", table_name="tutor_subscriptions")
    op.drop_index("ix_tutor_subscriptions_plan_id", table_name="tutor_subscriptions")
    op.drop_index("ix_tutor_subscriptions_tenant_id", table_name="tutor_subscriptions")
    op.drop_table("tutor_subscriptions")
    op.drop_index("ix_recurring_plans_tenant_id", table_name="recurring_plans")
    op.drop_table("recurring_plans")
