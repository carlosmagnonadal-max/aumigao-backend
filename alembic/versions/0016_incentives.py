"""Incentivos — regras configuraveis por tenant + amount/reward_type no incentivo

Aditivo, reversivel e idempotente (projeto usa schema-ensure/create_all).

- cria tabela incentive_rules (IF NOT EXISTS / checkfirst)
- ADD COLUMN IF NOT EXISTS amount + reward_type em walker_incentives

Revision ID: 0016_incentives
Revises: 0015_coupons
Create Date: 2026-06-10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0016_incentives"
down_revision: Union[str, None] = "0015_coupons"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return _inspector().has_table(name)


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return column_name in {col["name"] for col in _inspector().get_columns(table_name)}


def _create_table(name: str, *args, **kw) -> None:
    if not _has_table(name):
        op.create_table(name, *args, **kw)


def _create_index(index_name: str, table_name: str, columns, **kw) -> None:
    if not _has_table(table_name):
        return
    existing = {ix["name"] for ix in _inspector().get_indexes(table_name)}
    if index_name not in existing:
        op.create_index(index_name, table_name, columns, **kw)


def _add_column(table_name: str, column: sa.Column) -> None:
    if _has_table(table_name) and not _has_column(table_name, column.name):
        op.add_column(table_name, column)


def upgrade() -> None:
    _create_table(
        "incentive_rules",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("trigger_type", sa.String(), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False, server_default="0"),
        sa.Column("reward_type", sa.String(), nullable=False, server_default="recognition"),
        sa.Column("reward_value", sa.Float(), nullable=False, server_default="0"),
        sa.Column("visibility_effect", sa.String(), nullable=False, server_default="none"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "key", name="uq_incentive_rules_tenant_key"),
    )
    _create_index("ix_incentive_rules_tenant_id", "incentive_rules", ["tenant_id"])
    _create_index("ix_incentive_rules_key", "incentive_rules", ["key"])

    _add_column(
        "walker_incentives",
        sa.Column("reward_type", sa.String(), nullable=False, server_default="recognition"),
    )
    _add_column(
        "walker_incentives",
        sa.Column("amount", sa.Float(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    if _has_column("walker_incentives", "amount"):
        op.drop_column("walker_incentives", "amount")
    if _has_column("walker_incentives", "reward_type"):
        op.drop_column("walker_incentives", "reward_type")
    op.drop_table("incentive_rules")
