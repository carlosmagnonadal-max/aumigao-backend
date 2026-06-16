"""net-T1 — máquina de estados de convite em TenantWalkerAccess.

Adiciona timestamps do ciclo de convite à Rede Aumigão:
- invited_at  (datetime, nullable) — quando o convite foi emitido
- responded_at (datetime, nullable) — quando o passeador aceitou/recusou

Os estados (pending/active/declined/revoked) vivem na coluna `status` (já existente,
String), então não há nova coluna de status — apenas o conjunto de valores aceitos
muda no nível da aplicação (schema/serviço). Migration idempotente.

Revision ID: 0032_tenant_walker_access_invite_states
Revises: 0031_walker_presence
Create Date: 2026-06-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0032_tenant_walker_access_invite_states"
down_revision: Union[str, None] = "0031_walker_presence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "tenant_walker_access"


def _has_table(table: str) -> bool:
    return table in set(sa.inspect(op.get_bind()).get_table_names())


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if not _has_table(_TABLE):
        return
    if not _has_column(_TABLE, "invited_at"):
        op.add_column(_TABLE, sa.Column("invited_at", sa.DateTime(), nullable=True))
    if not _has_column(_TABLE, "responded_at"):
        op.add_column(_TABLE, sa.Column("responded_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    if not _has_table(_TABLE):
        return
    for col in ("responded_at", "invited_at"):
        if _has_column(_TABLE, col):
            op.drop_column(_TABLE, col)
