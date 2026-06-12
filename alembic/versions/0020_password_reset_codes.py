"""Cria tabela password_reset_codes para o fluxo de recuperação de senha mobile.

Revision ID: 0020_password_reset_codes
Revises: 0019_support_tickets
Create Date: 2026-06-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0020_password_reset_codes"
down_revision: Union[str, None] = "0019_support_tickets"
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
    if not _has_table("password_reset_codes"):
        op.create_table(
            "password_reset_codes",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("code_hash", sa.String(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("used_at", sa.DateTime(), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_password_reset_codes_user_id"),
            sa.PrimaryKeyConstraint("id"),
        )

    _create_index("ix_password_reset_codes_user_id", "password_reset_codes", ["user_id"])
    _create_index("ix_password_reset_codes_expires_at", "password_reset_codes", ["expires_at"])


def downgrade() -> None:
    if _has_table("password_reset_codes"):
        op.drop_table("password_reset_codes")
