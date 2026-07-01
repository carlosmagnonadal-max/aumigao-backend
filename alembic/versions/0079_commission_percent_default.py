"""commission_percent_default: neutraliza o server_default legado 20.0 da coluna
tenant_payment_configs.commission_percent → 10.0 (piso do plano Pro).

O 20.0 vinha da migration 0010 (era de 3 planos) e é MORTO: nenhum plano vigente
cobra 20%. Um registro criado sem valor explícito herdava 20% fantasma. Alinha o
DB ao modelo (DEFAULT_COMMISSION_PERCENT = 10.0). NÃO altera linhas existentes
(prod: todo registro real já tem commission_percent derivado do plano; e as com
20.0 estão marcadas commission_is_custom desde a 0028, então não devem ser tocadas
mesmo em backfills). Só troca o DEFAULT de futuras linhas sem valor.

Revision ID: 0079_commission_percent_default
Revises: 0078_coupon_redemption_unique
"""
from alembic import op

revision = "0079_commission_percent_default"
down_revision = "0078_coupon_redemption_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        op.alter_column(
            "tenant_payment_configs",
            "commission_percent",
            server_default="10.0",
        )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        op.alter_column(
            "tenant_payment_configs",
            "commission_percent",
            server_default="20.0",
        )
