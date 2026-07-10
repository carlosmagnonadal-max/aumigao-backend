"""0107 — motor financeiro de cancelamento (R14 itens 3/4).

Decisão do Carlos (10/07/2026): modelagem completa do cancelamento — config por
tenant, motivo gravado no walk, rastreio de estorno no payment, fila de
compensação do walker reusando walk_completion_reviews (kind).

Ver docs/superpowers/specs/2026-07-10-cancelamento-financeiro-design.md.

Idempotente (IF NOT EXISTS via inspect, padrão da 0103) — funciona em PG e
SQLite (ADD COLUMN com server_default).

Revision ID: 0107_walk_cancellation
Revises: 0106_cost_alerts
Create Date: 2026-07-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0107_walk_cancellation"
down_revision: Union[str, None] = "0106_cost_alerts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return _inspector().has_table(name)


def _has_column(table: str, column: str) -> bool:
    if not _has_table(table):
        return False
    existing = {c["name"] for c in _inspector().get_columns(table)}
    return column in existing


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    if not _has_table(table):
        return
    if not _has_column(table, column.name):
        op.add_column(table, column)


def upgrade() -> None:
    # ── walks: motivo do cancelamento GRAVADO (antes era descartado no client) ──
    _add_column_if_missing("walks", sa.Column("cancellation_reason_type", sa.String(), nullable=True))
    _add_column_if_missing("walks", sa.Column("cancellation_reason", sa.Text(), nullable=True))
    _add_column_if_missing("walks", sa.Column("cancelled_at", sa.DateTime(), nullable=True))
    _add_column_if_missing("walks", sa.Column("cancelled_by_role", sa.String(), nullable=True))

    # ── payments: rastreio do estorno pedido pelo motor de cancelamento ────────
    _add_column_if_missing("payments", sa.Column("refund_status", sa.String(), nullable=True))
    _add_column_if_missing(
        "payments", sa.Column("refunded_amount", sa.Numeric(12, 2), nullable=True)
    )

    # ── tenant_settings: config por tenant (padrão meeting_point_discount) ─────
    _add_column_if_missing(
        "tenant_settings",
        sa.Column(
            "cancellation_free_window_minutes",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1440"),
        ),
    )
    _add_column_if_missing(
        "tenant_settings",
        sa.Column(
            "late_cancellation_fee_percent",
            sa.Numeric(5, 2),
            nullable=False,
            server_default=sa.text("50"),
        ),
    )
    _add_column_if_missing(
        "tenant_settings",
        sa.Column(
            "late_fee_walker_share_percent",
            sa.Numeric(5, 2),
            nullable=False,
            server_default=sa.text("100"),
        ),
    )
    _add_column_if_missing(
        "tenant_settings",
        sa.Column(
            "auto_refund_on_cancel",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )

    # ── walk_completion_reviews: reusa a MESMA fila de aprovação para a
    # compensação de cancelamento do walker (kind distingue de "completion") ──
    _add_column_if_missing(
        "walk_completion_reviews",
        sa.Column("kind", sa.String(), nullable=False, server_default="completion"),
    )
    _add_column_if_missing(
        "walk_completion_reviews",
        sa.Column("compensation_amount", sa.Numeric(12, 2), nullable=True),
    )


def downgrade() -> None:
    _drop_if_exists = lambda table, column: op.drop_column(table, column) if _has_column(table, column) else None
    _drop_if_exists("walk_completion_reviews", "compensation_amount")
    _drop_if_exists("walk_completion_reviews", "kind")
    _drop_if_exists("tenant_settings", "auto_refund_on_cancel")
    _drop_if_exists("tenant_settings", "late_fee_walker_share_percent")
    _drop_if_exists("tenant_settings", "late_cancellation_fee_percent")
    _drop_if_exists("tenant_settings", "cancellation_free_window_minutes")
    _drop_if_exists("payments", "refunded_amount")
    _drop_if_exists("payments", "refund_status")
    _drop_if_exists("walks", "cancelled_by_role")
    _drop_if_exists("walks", "cancelled_at")
    _drop_if_exists("walks", "cancellation_reason")
    _drop_if_exists("walks", "cancellation_reason_type")
