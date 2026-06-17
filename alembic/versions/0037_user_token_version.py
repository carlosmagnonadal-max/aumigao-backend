"""B-ALT-011 (passo 2b) — revogacao de sessao via token_version.

users: adiciona token_version (Integer, NOT NULL, server_default "0"). Default 0 =>
tokens legados (sem "ver") seguem aceitos; novos tokens carregam ver=0 ate a 1a troca de
senha. ZERO regressao no deploy.

Revision ID: 0037_user_token_version
Revises: 0036_fk_integrity
Create Date: 2026-06-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0037_user_token_version"
down_revision: Union[str, None] = "0036_fk_integrity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "users"
_COLUMN = "token_version"


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if not _has_column(_TABLE, _COLUMN):
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    if _has_column(_TABLE, _COLUMN):
        op.drop_column(_TABLE, _COLUMN)
