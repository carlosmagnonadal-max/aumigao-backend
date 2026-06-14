"""Comissão por plano (10/8/5) + flag de override manual.

Aditivo e reversível. Adiciona commission_is_custom em tenant_payment_configs,
protege comissões negociadas (qualquer valor != default legado 20.0) marcando-as
como custom, e faz backfill das demais conforme o tier do plano do tenant.

Revision ID: 0028_commission_by_plan
Revises: 0027_shared_duration_pricing
Create Date: 2026-06-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0028_commission_by_plan"
down_revision: Union[str, None] = "0027_shared_duration_pricing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "tenant_payment_configs"


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    insp = sa.inspect(op.get_bind())
    existing = {c["name"] for c in insp.get_columns(table)}
    if column.name not in existing:
        op.add_column(table, column)


def upgrade() -> None:
    _add_column_if_missing(
        _TABLE,
        sa.Column("commission_is_custom", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # Backfill de dados só no Postgres (prod/Neon). Em sqlite (testes) o schema é
        # criado via create_all; o backfill não se aplica.
        return

    # 1) Protege comissões negociadas: tudo que NÃO está no default legado (20.0)
    #    foi setado à mão (ex.: Fundador/sócio 0%) -> marca como custom p/ não sobrescrever.
    op.execute(
        f"UPDATE {_TABLE} SET commission_is_custom = true WHERE commission_percent <> 20.0"
    )

    # 2) Backfill dos defaults por plano apenas para os NÃO-custom (estavam em 20.0).
    op.execute(
        f"""
        UPDATE {_TABLE} AS tpc
        SET commission_percent = CASE lower(t.plan)
            WHEN 'starter' THEN 10
            WHEN 'business' THEN 8
            WHEN 'enterprise' THEN 5
            ELSE 10
        END
        FROM tenants AS t
        WHERE t.id = tpc.tenant_id AND tpc.commission_is_custom = false
        """
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "commission_is_custom")
