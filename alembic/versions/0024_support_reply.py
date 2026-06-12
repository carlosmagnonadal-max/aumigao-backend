"""Fase 4 C1 — suporte real: user_id + reply + replied_at nos tickets.

support_tickets:
  - user_id (VARCHAR nullable, FK users.id, index) — autor do ticket no app
  - reply (TEXT nullable)                          — resposta pública ao usuário
  - replied_at (DATETIME nullable)                 — quando a resposta foi enviada

Revision ID: 0024_support_reply
Revises: 0023_split_fields
Create Date: 2026-06-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0024_support_reply"
down_revision: Union[str, None] = "0023_split_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_column(table: str, column: str) -> bool:
    cols = {c["name"] for c in _inspector().get_columns(table)}
    return column in cols


def _has_index(table: str, index_name: str) -> bool:
    indexes = {i["name"] for i in _inspector().get_indexes(table)}
    return index_name in indexes


def upgrade() -> None:
    # user_id — autor do ticket (FK users.id, nullable)
    if not _has_column("support_tickets", "user_id"):
        op.add_column(
            "support_tickets",
            sa.Column("user_id", sa.String(), nullable=True),
        )

    if not _has_index("support_tickets", "ix_support_tickets_user_id"):
        try:
            op.create_index(
                "ix_support_tickets_user_id", "support_tickets", ["user_id"]
            )
        except Exception:
            pass

    # reply — resposta pública ao usuário
    if not _has_column("support_tickets", "reply"):
        op.add_column(
            "support_tickets",
            sa.Column("reply", sa.Text(), nullable=True),
        )

    # replied_at — timestamp de quando a resposta foi enviada
    if not _has_column("support_tickets", "replied_at"):
        op.add_column(
            "support_tickets",
            sa.Column("replied_at", sa.DateTime(), nullable=True),
        )


def downgrade() -> None:
    insp = _inspector()
    existing_cols = {c["name"] for c in insp.get_columns("support_tickets")}

    if "replied_at" in existing_cols:
        op.drop_column("support_tickets", "replied_at")

    if "reply" in existing_cols:
        op.drop_column("support_tickets", "reply")

    # Remove índice antes da coluna
    try:
        existing_indexes = {i["name"] for i in insp.get_indexes("support_tickets")}
        if "ix_support_tickets_user_id" in existing_indexes:
            op.drop_index("ix_support_tickets_user_id", table_name="support_tickets")
    except Exception:
        pass

    if "user_id" in existing_cols:
        op.drop_column("support_tickets", "user_id")
