"""walker_earnings: colunas de estorno (void_reason, voided_at) — Fase 3

Revision ID: 0065_walker_earning_void
Revises: 0064_walker_earnings
"""
import sqlalchemy as sa
from alembic import op

revision = "0065_walker_earning_void"
down_revision = "0064_walker_earnings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("walker_earnings", sa.Column("void_reason", sa.String(), nullable=True))
    op.add_column("walker_earnings", sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("walker_earnings", "voided_at")
    op.drop_column("walker_earnings", "void_reason")
