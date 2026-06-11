"""Persiste configuracoes de programa no banco (app_settings + walker_program_actions).

Corrige o bug em que REFERRAL_PROGRAM_SETTINGS e WALKER_PROGRAM_SETTINGS viviam em
memoria e sumiam a cada deploy do Railway. Tambem persiste WALKER_PROGRAM_ACTIONS
que antes era uma lista em memoria.

Revision ID: 0018_persist_program_settings
Revises: 0017_contact_messages
Create Date: 2026-06-11
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0018_persist_program_settings"
down_revision: Union[str, None] = "0017_contact_messages"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return _inspector().has_table(name)


def _create_table(name: str, *args, **kw) -> None:
    if not _has_table(name):
        op.create_table(name, *args, **kw)


def _create_index(index_name: str, table_name: str, columns, **kw) -> None:
    if not _has_table(table_name):
        return
    existing = {ix["name"] for ix in _inspector().get_indexes(table_name)}
    if index_name not in existing:
        op.create_index(index_name, table_name, columns, **kw)


def upgrade() -> None:
    # Tabela de configuracoes administrativas (key-value JSON)
    _create_table(
        "app_settings",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("key"),
    )

    # Tabela de acoes do programa de passeadores (imutavel, append-only)
    _create_table(
        "walker_program_actions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("action_type", sa.String(), nullable=False),
        sa.Column("walker_id", sa.String(), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    _create_index(
        "ix_walker_program_actions_created_at",
        "walker_program_actions",
        ["created_at"],
    )
    _create_index(
        "ix_walker_program_actions_action_type",
        "walker_program_actions",
        ["action_type"],
    )
    _create_index(
        "ix_walker_program_actions_walker_id",
        "walker_program_actions",
        ["walker_id"],
    )


def downgrade() -> None:
    if _has_table("walker_program_actions"):
        op.drop_table("walker_program_actions")
    if _has_table("app_settings"):
        op.drop_table("app_settings")
