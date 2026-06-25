"""walker_profiles.pix_key — chave Pix self-service do passeador

Revision ID: 0054_walker_pix_key
Revises: 0053_tenant_tutor_access
Create Date: 2026-06-25
"""
from __future__ import annotations
from typing import Union
import sqlalchemy as sa
from alembic import op

revision: str = "0054_walker_pix_key"
down_revision: Union[str, None] = "0053_tenant_tutor_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "walker_profiles",
        sa.Column("pix_key", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("walker_profiles", "pix_key")
