"""tutor_subscription_cancel_reason: motivo do cancelamento da assinatura do tutor.

Opção B (aprovada pelo Carlos): créditos JÁ PAGOS de assinatura cancelada pelo
DOWNGRADE do reverse trial permanecem consumíveis até esgotar. Para distinguir
esse caso do cancelamento manual (forfeit + breakage imediato, inalterado):

Coluna ADITIVA em `tutor_subscriptions` (sem tabela nova → sem RLS nova):
  - cancel_reason  VARCHAR NULL  → 'plan_downgrade' quando o cancelamento veio do
                                    downgrade do trial; NULL nos demais caminhos.

Semântica ligada à coluna:
  - consume_credit_if_available honra CANCELLED apenas com reason='plan_downgrade';
  - sweep_expired_credits (breakage) PULA CANCELLED com reason='plan_downgrade'
    (o passivo persiste enquanto o crédito for resgatável);
  - refund_credit_for_walk devolve crédito também a CANCELLED-downgrade.

Zero-regressão: NULL para todas as assinaturas existentes → canceladas manuais
continuam forfeit (não-consumíveis), ativas seguem idênticas.

Revision ID: 0089_tutor_subscription_cancel_reason
Revises: 0088_walker_pet_photo_url
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0089_tutor_subscription_cancel_reason"
down_revision = "0088_walker_pet_photo_url"
branch_labels = None
depends_on = None

_TABLE = "tutor_subscriptions"
_COLUMN = "cancel_reason"


def _existing_columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if _COLUMN not in _existing_columns(_TABLE):
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(), nullable=True))


def downgrade() -> None:
    if _COLUMN in _existing_columns(_TABLE):
        op.drop_column(_TABLE, _COLUMN)
