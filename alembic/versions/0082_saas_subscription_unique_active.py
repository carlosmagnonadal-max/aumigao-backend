"""saas_subscription_unique_active: garante <=1 assinatura SaaS "viva" por tenant.

Defesa de banco além do código: um tenant não pode ter duas TenantSaasSubscription
simultaneamente ativas/inadimplentes (cobrança dupla). Índice único PARCIAL em
tenant_id WHERE status IN ('active','overdue') — 'cancelled' fica de fora
(histórico admite várias canceladas).

Prod: a tabela está vazia (nenhuma assinatura SaaS iniciada — gate = 1º cliente
real), então não há risco de violar dados existentes. SQLite (testes) suporta
índice único parcial com WHERE; criamos em ambos os dialetos.

Revision ID: 0082_saas_subscription_unique_active
Revises: 0081_walker_referral_tenant_id
"""
from alembic import op
import sqlalchemy as sa

revision = "0082_saas_subscription_unique_active"
down_revision = "0081_walker_referral_tenant_id"
branch_labels = None
depends_on = None

_INDEX = "uq_tenant_saas_subscriptions_active_per_tenant"
_WHERE = "status IN ('active', 'overdue')"


def upgrade() -> None:
    op.create_index(
        _INDEX,
        "tenant_saas_subscriptions",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text(_WHERE),
        sqlite_where=sa.text(_WHERE),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="tenant_saas_subscriptions")
