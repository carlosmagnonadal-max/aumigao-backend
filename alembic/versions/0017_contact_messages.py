"""Contact messages — leads do formulário de contato do site (intake público)

Aditivo, reversível e idempotente (projeto usa schema-ensure/create_all).

Revision ID: 0017_contact_messages
Revises: 0016_incentives
Create Date: 2026-06-10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0017_contact_messages"
down_revision: Union[str, None] = "0016_incentives"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return _inspector().has_table(name)


def _create_table(name: str, *args, **kw) -> None:
    if not _has_table(name):
        op.create_table(name, *args, **kw)


def _create_index(index_name: str, table_name: str, columns, **kw) -> None:
    if not _has_table(table_name):
        return
    existing = {ix["name"] for ix in _inspector().get_indexes(table_name)}
    if index_name not in existing:
        op.create_index(index_name, table_name, columns, **kw)


def upgrade() -> None:
    _create_table(
        "contact_messages",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=False, server_default=""),
        sa.Column("company", sa.String(), nullable=False, server_default=""),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("phone", sa.String(), nullable=False, server_default=""),
        sa.Column("city", sa.String(), nullable=False, server_default=""),
        sa.Column("business_type", sa.String(), nullable=False, server_default=""),
        sa.Column("interest", sa.String(), nullable=False, server_default=""),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(), nullable=False, server_default="site"),
        sa.Column("status", sa.String(), nullable=False, server_default="new"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    _create_index("ix_contact_messages_tenant_id", "contact_messages", ["tenant_id"])
    _create_index("ix_contact_messages_email", "contact_messages", ["email"])
    _create_index("ix_contact_messages_source", "contact_messages", ["source"])
    _create_index("ix_contact_messages_status", "contact_messages", ["status"])
    _create_index("ix_contact_messages_created_at", "contact_messages", ["created_at"])


def downgrade() -> None:
    if _has_table("contact_messages"):
        op.drop_table("contact_messages")
