"""Cria tabela support_tickets para tickets de suporte interno.

Revision ID: 0019_support_tickets
Revises: 0018_persist_program_settings
Create Date: 2026-06-11
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0019_support_tickets"
down_revision: Union[str, None] = "0018_persist_program_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return _inspector().has_table(name)


def _create_index(index_name: str, table_name: str, columns, **kw) -> None:
    if not _has_table(table_name):
        return
    existing = {ix["name"] for ix in _inspector().get_indexes(table_name)}
    if index_name not in existing:
        op.create_index(index_name, table_name, columns, **kw)


def upgrade() -> None:
    if not _has_table("support_tickets"):
        op.create_table(
            "support_tickets",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("tenant_id", sa.String(), nullable=True),
            sa.Column("subject", sa.String(), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("requester_name", sa.String(), nullable=True),
            sa.Column("requester_email", sa.String(), nullable=True),
            sa.Column("requester_role", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=False, server_default="open"),
            sa.Column("priority", sa.String(), nullable=False, server_default="normal"),
            sa.Column("assignee_user_id", sa.String(), nullable=True),
            sa.Column("internal_notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_support_tickets_tenant_id"),
            sa.PrimaryKeyConstraint("id"),
        )

    _create_index("ix_support_tickets_tenant_id", "support_tickets", ["tenant_id"])
    _create_index("ix_support_tickets_status", "support_tickets", ["status"])
    _create_index("ix_support_tickets_created_at", "support_tickets", ["created_at"])


def downgrade() -> None:
    if _has_table("support_tickets"):
        op.drop_table("support_tickets")
