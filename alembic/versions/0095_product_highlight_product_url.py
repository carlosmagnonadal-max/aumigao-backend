"""product_highlight_product_url — adiciona product_url em tenant_product_highlights.

Coluna ADITIVA e NULL (zero default disruptivo): link do produto no site do tenant.
Segue o padrão has_column das migrations anteriores (0094, 0088...).

Revision ID: 0095_product_highlight_product_url
Revises: 0094_pet_profile_p0
Create Date: 2026-07-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0095_product_highlight_product_url"
down_revision: Union[str, None] = "0094_pet_profile_p0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "tenant_product_highlights"
_COLUMN = "product_url"


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if not _has_column(_TABLE, _COLUMN):
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(2000), nullable=True))


def downgrade() -> None:
    if _has_column(_TABLE, _COLUMN):
        op.drop_column(_TABLE, _COLUMN)
