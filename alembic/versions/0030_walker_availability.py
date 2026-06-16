"""WK-01 — tabela de disponibilidade semanal do passeador.

walker_availability: uma linha por passeador, schedule editável em JSON.
Antes a disponibilidade só existia em AsyncStorage local no app.

Revision ID: 0030_walker_availability
Revises: 0029_plan_discount_percent
Create Date: 2026-06-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0030_walker_availability"
down_revision: Union[str, None] = "0029_plan_discount_percent"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "walker_availability"


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    if _has_table(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("walker_user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("schedule_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("walker_user_id", name="uq_walker_availability_walker"),
    )
    op.create_index("ix_walker_availability_walker_user_id", _TABLE, ["walker_user_id"])


def downgrade() -> None:
    if not _has_table(_TABLE):
        return
    try:
        op.drop_index("ix_walker_availability_walker_user_id", table_name=_TABLE)
    except Exception:
        pass
    op.drop_table(_TABLE)
