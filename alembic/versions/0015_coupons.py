"""Onda 2 — cupons: coupons + coupon_redemptions

Aditivo, reversível e idempotente (projeto usa schema-ensure/create_all).

Revision ID: 0015_coupons
Revises: 0014_shared_walks
Create Date: 2026-06-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0015_coupons"
down_revision: Union[str, None] = "0014_shared_walks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _create_table(name: str, *args, **kw) -> None:
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
        "coupons",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("discount_type", sa.String(), nullable=False, server_default="percent"),
        sa.Column("discount_value", sa.Float(), nullable=False, server_default="0"),
        sa.Column("min_amount", sa.Float(), nullable=False, server_default="0"),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("max_uses_per_user", sa.Integer(), nullable=True),
        sa.Column("uses_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("valid_from", sa.DateTime(), nullable=True),
        sa.Column("valid_until", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_coupons_tenant_code"),
    )
    _create_index("ix_coupons_tenant_id", "coupons", ["tenant_id"])
    _create_index("ix_coupons_code", "coupons", ["code"])

    _create_table(
        "coupon_redemptions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("coupon_id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("walk_id", sa.String(), nullable=True),
        sa.Column("amount_discounted", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["coupon_id"], ["coupons.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    _create_index("ix_coupon_redemptions_coupon_id", "coupon_redemptions", ["coupon_id"])
    _create_index("ix_coupon_redemptions_tenant_id", "coupon_redemptions", ["tenant_id"])
    _create_index("ix_coupon_redemptions_user_id", "coupon_redemptions", ["user_id"])


def downgrade() -> None:
    op.drop_table("coupon_redemptions")
    op.drop_table("coupons")
