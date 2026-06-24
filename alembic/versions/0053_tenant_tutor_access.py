"""tenant_tutor_access (Modelo B white-label) — tabela aditiva + RLS via .sql

Revision ID: 0053_tenant_tutor_access
Revises: 0052_tenant_walker_requirements
Create Date: 2026-06-24
"""
from __future__ import annotations
from typing import Union
import sqlalchemy as sa
from alembic import op

revision: str = "0053_tenant_tutor_access"
down_revision: Union[str, None] = "0052_tenant_walker_requirements"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def upgrade() -> None:
    if _has_table("tenant_tutor_access"):
        return
    op.create_table(
        "tenant_tutor_access",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("tutor_user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("initiated_by", sa.String(length=16), nullable=False, server_default="tutor"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("tenant_id", "tutor_user_id", name="uq_tenant_tutor_access_tenant_tutor"),
    )
    op.create_index("ix_tenant_tutor_access_tenant_id", "tenant_tutor_access", ["tenant_id"])
    op.create_index("ix_tenant_tutor_access_tutor_user_id", "tenant_tutor_access", ["tutor_user_id"])
    op.create_index("ix_tenant_tutor_access_status", "tenant_tutor_access", ["status"])


def downgrade() -> None:
    if _has_table("tenant_tutor_access"):
        op.drop_table("tenant_tutor_access")
