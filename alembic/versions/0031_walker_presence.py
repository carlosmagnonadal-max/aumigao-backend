"""WK-02 — presença online do passeador.

walker_profiles: adiciona is_online (bool, default false) e last_seen_at (datetime).

Revision ID: 0031_walker_presence
Revises: 0030_walker_availability
Create Date: 2026-06-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0031_walker_presence"
down_revision: Union[str, None] = "0030_walker_availability"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "walker_profiles"


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if not _has_column(_TABLE, "is_online"):
        op.add_column(_TABLE, sa.Column("is_online", sa.Boolean(), nullable=False, server_default="false"))
    if not _has_column(_TABLE, "last_seen_at"):
        op.add_column(_TABLE, sa.Column("last_seen_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    for col in ("last_seen_at", "is_online"):
        if _has_column(_TABLE, col):
            op.drop_column(_TABLE, col)
