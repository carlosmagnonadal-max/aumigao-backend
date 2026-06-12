"""Cria tabela walk_location_pings para rastreamento GPS ao vivo de passeios.

Revision ID: 0021_walk_location_pings
Revises: 0020_password_reset_codes
Create Date: 2026-06-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0021_walk_location_pings"
down_revision: Union[str, None] = "0020_password_reset_codes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return _inspector().has_table(name)


def _create_index(index_name: str, table_name: str, columns, **kw) -> None:
    if not _has_table(table_name):
        return
    existing = {ix["name"] for ix in _inspector().get_indexes(table_name)}
    if index_name not in existing:
        op.create_index(index_name, table_name, columns, **kw)


def upgrade() -> None:
    if not _has_table("walk_location_pings"):
        op.create_table(
            "walk_location_pings",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("walk_id", sa.String(), nullable=False),
            sa.Column("walker_id", sa.String(), nullable=False),
            sa.Column("latitude", sa.Float(), nullable=False),
            sa.Column("longitude", sa.Float(), nullable=False),
            sa.Column("accuracy", sa.Float(), nullable=True),
            sa.Column("recorded_at", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["walk_id"], ["walks.id"], name="fk_walk_location_pings_walk_id"),
            sa.ForeignKeyConstraint(["walker_id"], ["users.id"], name="fk_walk_location_pings_walker_id"),
            sa.PrimaryKeyConstraint("id"),
        )

    _create_index("ix_walk_location_pings_walk_id", "walk_location_pings", ["walk_id"])
    _create_index("ix_walk_location_pings_walker_id", "walk_location_pings", ["walker_id"])
    _create_index("ix_walk_location_pings_recorded_at", "walk_location_pings", ["recorded_at"])
    _create_index("ix_walk_location_pings_walk_id_recorded_at", "walk_location_pings", ["walk_id", "recorded_at"])


def downgrade() -> None:
    if _has_table("walk_location_pings"):
        op.drop_table("walk_location_pings")
