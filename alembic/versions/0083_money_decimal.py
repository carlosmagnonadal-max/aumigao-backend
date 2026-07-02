"""Governança de dinheiro (parte 2) — Float -> Numeric nos campos monetários restantes.

Continua o 0033_money_numeric: aplica NUMERIC aos VALORES em R$ que ainda eram
Float (ledgers de comissão/ganho, preços de plano/assinatura/passeio, cupons,
incentivos, gorjeta, referral) e NUMERIC(5,2) aos PERCENTUAIS de comissão em
snapshot. Armazenamento exato no Postgres, sem drift de ponto flutuante. O cálculo
em Python já opera em Decimal (app.core.money) e o tipo Money/Money4
(TypeDecorator) arredonda a centavos em qualquer dialeto.

Contexto de produção: os ledgers (commission_entries, walker_earnings) e as
assinaturas estão VAZIOS (0 linhas), então o ALTER COLUMN não converte dado —
é uma troca de tipo trivial e segura. Ainda assim, aplicar em janela de baixa carga.

Só roda no PostgreSQL — em SQLite a affinity NUMERIC já é compatível (no-op).
NÃO toca colunas que já eram Numeric/Decimal (tenant_saas_subscriptions.price,
credit_ledger_entries via 0033? não — ver abaixo; payment_provision/fiscal já usam
o tipo Money; tenant_walker_access.commission_percent já é Numeric(5,2)).

Revision ID: 0083_money_decimal
Revises: 0082_saas_subscription_unique_active
Create Date: 2026-07-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0083_money_decimal"
down_revision: Union[str, None] = "0082_saas_subscription_unique_active"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (tabela, coluna, precisão, escala, nullable) — VALORES em reais (numeric(12,2)),
# valor unitário do crédito com 4 casas (numeric(12,4)) e percentuais em snapshot
# (numeric(5,2)).
_MONEY_2 = [
    ("commission_entries", "walk_price", False),
    ("commission_entries", "amount", False),
    ("walker_earnings", "gross", False),
    ("walker_earnings", "platform_amount", False),
    ("walker_earnings", "amount", False),
    ("recurring_plans", "price", False),
    ("tutor_subscriptions", "price", False),
    ("tenant_individual_walk_pricing", "price_30", False),
    ("tenant_individual_walk_pricing", "price_45", False),
    ("tenant_individual_walk_pricing", "price_60", False),
    ("tenant_shared_walk_configs", "price_per_pet", False),
    ("tenant_shared_walk_configs", "price_30", False),
    ("tenant_shared_walk_configs", "price_45", False),
    ("tenant_shared_walk_configs", "price_60", False),
    ("shared_walks", "price_per_pet", True),
    ("shared_walk_participants", "price", True),
    ("tenant_pet_tour_configs", "base_price", False),
    ("coupons", "discount_value", False),
    ("coupons", "min_amount", False),
    ("coupon_redemptions", "amount_discounted", False),
    ("credit_ledger_entries", "total_value", True),
    ("incentive_rules", "reward_value", False),
    ("walker_incentives", "amount", True),
    ("tip_integrity_flags", "tip_amount", True),
    ("tutor_referral_configs", "discount_value", False),
    ("walker_referrals", "reward_amount", True),
]

_MONEY_4 = [
    ("credit_ledger_entries", "unit_value", True),
]

_PERCENT_2 = [
    ("commission_entries", "commission_percent", False),
    ("payments", "commission_percent", True),
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table, col, nullable in _MONEY_2:
        op.alter_column(
            table, col,
            type_=sa.Numeric(12, 2),
            existing_type=sa.Float(),
            postgresql_using=f"{col}::numeric(12,2)",
            existing_nullable=nullable,
        )
    for table, col, nullable in _MONEY_4:
        op.alter_column(
            table, col,
            type_=sa.Numeric(12, 4),
            existing_type=sa.Float(),
            postgresql_using=f"{col}::numeric(12,4)",
            existing_nullable=nullable,
        )
    for table, col, nullable in _PERCENT_2:
        op.alter_column(
            table, col,
            type_=sa.Numeric(5, 2),
            existing_type=sa.Float(),
            postgresql_using=f"{col}::numeric(5,2)",
            existing_nullable=nullable,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table, col, nullable in _MONEY_2 + _MONEY_4 + _PERCENT_2:
        op.alter_column(
            table, col,
            type_=sa.Float(),
            existing_type=sa.Numeric(),
            postgresql_using=f"{col}::double precision",
            existing_nullable=nullable,
        )
