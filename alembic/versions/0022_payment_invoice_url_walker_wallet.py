"""Adiciona invoice_url em payments e asaas_wallet_id em walker_profiles.

invoice_url: persiste a URL do checkout/fatura Asaas para exibição offline.
asaas_wallet_id: carteira Asaas do walker para split real no modo live (dormente).

Revision ID: 0022_payment_invoice_url_walker_wallet
Revises: 0021_walk_location_pings
Create Date: 2026-06-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0022_payment_invoice_url_walker_wallet"
down_revision: Union[str, None] = "0021_walk_location_pings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_column(table: str, column: str) -> bool:
    cols = {c["name"] for c in _inspector().get_columns(table)}
    return column in cols


def upgrade() -> None:
    # payments.invoice_url
    if not _has_column("payments", "invoice_url"):
        op.add_column(
            "payments",
            sa.Column("invoice_url", sa.String(), nullable=True),
        )

    # walker_profiles.asaas_wallet_id
    if not _has_column("walker_profiles", "asaas_wallet_id"):
        op.add_column(
            "walker_profiles",
            sa.Column("asaas_wallet_id", sa.String(), nullable=True),
        )


def downgrade() -> None:
    # walker_profiles.asaas_wallet_id
    insp = _inspector()
    if "asaas_wallet_id" in {c["name"] for c in insp.get_columns("walker_profiles")}:
        op.drop_column("walker_profiles", "asaas_wallet_id")

    # payments.invoice_url
    if "invoice_url" in {c["name"] for c in insp.get_columns("payments")}:
        op.drop_column("payments", "invoice_url")
