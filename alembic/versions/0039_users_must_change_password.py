"""B2: adiciona must_change_password em users.

Flag que forca troca de senha no 1o login de admins criados via POST /admin/accounts.
Setada True na criacao; zerada apos troca bem-sucedida via /auth/change-password.

Revision ID: 0039_users_must_change_password
Revises: 0038_walker_background_check
Create Date: 2026-06-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0039_users_must_change_password"
down_revision: Union[str, None] = "0038_walker_background_check"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "users"
_COLUMN = "must_change_password"


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if not _has_column(_TABLE, _COLUMN):
        op.add_column(
            _TABLE,
            sa.Column(
                _COLUMN,
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )


def downgrade() -> None:
    if _has_column(_TABLE, _COLUMN):
        op.drop_column(_TABLE, _COLUMN)
