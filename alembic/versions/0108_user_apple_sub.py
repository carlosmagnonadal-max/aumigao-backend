"""0108 — users.apple_sub (SEC: âncora de identidade Apple Sign-In).

Correção de account-takeover no Apple Sign-In: passa a resolver a conta pelo
`sub` do identity_token (estável e assinado), nunca pelo e-mail client-supplied.
Esta coluna guarda esse `sub`.

Coluna ADITIVA e NULL (zero default disruptivo) + índice ÚNICO. Idempotente
(padrão has_column/_create_index das migrations 0107/0014) — funciona em PG e
SQLite.

Revision ID: 0108_user_apple_sub
Revises: 0107_walk_cancellation
Create Date: 2026-07-16
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0108_user_apple_sub"
down_revision: Union[str, None] = "0107_walk_cancellation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "users"
_COLUMN = "apple_sub"
_INDEX = "ix_users_apple_sub"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in _inspector().get_columns(table)}


def _has_index(table: str, index_name: str) -> bool:
    return index_name in {ix["name"] for ix in _inspector().get_indexes(table)}


def upgrade() -> None:
    if not _has_column(_TABLE, _COLUMN):
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(), nullable=True))
    if not _has_index(_TABLE, _INDEX):
        op.create_index(_INDEX, _TABLE, [_COLUMN], unique=True)


def downgrade() -> None:
    if _has_index(_TABLE, _INDEX):
        op.drop_index(_INDEX, table_name=_TABLE)
    if _has_column(_TABLE, _COLUMN):
        op.drop_column(_TABLE, _COLUMN)
