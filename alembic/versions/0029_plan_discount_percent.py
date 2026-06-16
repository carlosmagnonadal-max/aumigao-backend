"""R4 — desconto de plano por tenant.

tenant_payment_configs: adiciona plan_discount_percent (float, default 0.0).
% de desconto que o tenant concede por passeio aos tutores do seu plano recorrente,
configurável no admin. Default 0 = sem desconto (idêntico ao comportamento anterior).

Revision ID: 0029_plan_discount_percent
Revises: 0028_commission_by_plan
Create Date: 2026-06-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0029_plan_discount_percent"
down_revision: Union[str, None] = "0028_commission_by_plan"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "tenant_payment_configs"
_COLUMN = "plan_discount_percent"


def _has_column(table: str, column: str) -> bool:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}
    return column in cols


def upgrade() -> None:
    if not _has_column(_TABLE, _COLUMN):
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.Float(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    if _has_column(_TABLE, _COLUMN):
        op.drop_column(_TABLE, _COLUMN)
