"""0110 — Capacitação do passeador (S2): walker_training_progress + walker_profiles.training_completed_*.

Tabela GLOBAL (sem tenant_id, sem RLS): passeador é rede global, igual a walker_profiles /
walker_kit_submissions / walker_network_profiles (fora da lista da 0043).
Colunas ADITIVAS e NULL. Idempotente (padrão _has_* da 0107/0108) — PG e SQLite.
Produção: aplicar via scripts/db/0110_walker_training.sql no Neon SQL Editor.

Revision ID: 0110_walker_training
Revises: 0109_walk_emergency_calls
Create Date: 2026-09-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0110_walker_training"
down_revision: Union[str, None] = "0109_walk_emergency_calls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "walker_training_progress"
_INDEX = "ix_walker_training_progress_walker_user_id"
_PROFILE_COLUMNS = (
    ("training_completed_version", sa.String()),
    ("training_completed_at", sa.DateTime()),
)


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return _inspector().has_table(name)


def _has_column(table: str, column: str) -> bool:
    return _has_table(table) and column in {c["name"] for c in _inspector().get_columns(table)}


def _has_index(table: str, index_name: str) -> bool:
    return _has_table(table) and index_name in {ix["name"] for ix in _inspector().get_indexes(table)}


def upgrade() -> None:
    if not _has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("walker_user_id", sa.String(), nullable=False),
            sa.Column("content_version", sa.String(), nullable=False),
            sa.Column("module_id", sa.String(), nullable=False),
            sa.Column("best_score", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("passed_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("walker_user_id", "content_version", "module_id",
                                name="uq_walker_training_progress_module"),
        )
    if not _has_index(_TABLE, _INDEX):
        op.create_index(_INDEX, _TABLE, ["walker_user_id"])
    for column, column_type in _PROFILE_COLUMNS:
        if not _has_column("walker_profiles", column):
            op.add_column("walker_profiles", sa.Column(column, column_type, nullable=True))


def downgrade() -> None:
    for column, _ in reversed(_PROFILE_COLUMNS):
        if _has_column("walker_profiles", column):
            op.drop_column("walker_profiles", column)
    if _has_index(_TABLE, _INDEX):
        op.drop_index(_INDEX, table_name=_TABLE)
    if _has_table(_TABLE):
        op.drop_table(_TABLE)
