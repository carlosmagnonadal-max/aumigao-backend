"""Fase 7 $-2 — gorjeta real via Asaas + assinaturas recorrentes nativas.

walk_tips:
  - provider_payment_id (VARCHAR nullable)  — ID do pagamento no Asaas
  - invoice_url (VARCHAR nullable)          — URL de fatura/checkout retornada pelo Asaas

tutor_subscriptions:
  - asaas_subscription_id (VARCHAR nullable) — ID da subscription no Asaas

Revision ID: 0025_money_fields
Revises: 0024_support_reply
Create Date: 2026-06-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0025_money_fields"
down_revision: Union[str, None] = "0024_support_reply"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_column(table: str, column: str) -> bool:
    cols = {c["name"] for c in _inspector().get_columns(table)}
    return column in cols


def upgrade() -> None:
    # walk_tips: provider_payment_id
    if not _has_column("walk_tips", "provider_payment_id"):
        op.add_column(
            "walk_tips",
            sa.Column("provider_payment_id", sa.String(), nullable=True),
        )

    # walk_tips: invoice_url
    if not _has_column("walk_tips", "invoice_url"):
        op.add_column(
            "walk_tips",
            sa.Column("invoice_url", sa.String(), nullable=True),
        )

    # tutor_subscriptions: asaas_subscription_id
    if not _has_column("tutor_subscriptions", "asaas_subscription_id"):
        op.add_column(
            "tutor_subscriptions",
            sa.Column("asaas_subscription_id", sa.String(), nullable=True),
        )


def downgrade() -> None:
    insp = _inspector()

    tips_cols = {c["name"] for c in insp.get_columns("walk_tips")}
    if "invoice_url" in tips_cols:
        op.drop_column("walk_tips", "invoice_url")
    if "provider_payment_id" in tips_cols:
        op.drop_column("walk_tips", "provider_payment_id")

    sub_cols = {c["name"] for c in insp.get_columns("tutor_subscriptions")}
    if "asaas_subscription_id" in sub_cols:
        op.drop_column("tutor_subscriptions", "asaas_subscription_id")
