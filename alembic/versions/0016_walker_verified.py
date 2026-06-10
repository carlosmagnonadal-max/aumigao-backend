"""Onda 2 — passeador verificado: walker_profiles.verified (+ at/by)

Aditivo, reversível e idempotente (ADD COLUMN IF NOT EXISTS).

Revision ID: 0016_walker_verified
Revises: 0015_coupons
Create Date: 2026-06-09
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0016_walker_verified"
down_revision: Union[str, None] = "0015_coupons"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE walker_profiles ADD COLUMN IF NOT EXISTS verified BOOLEAN NOT NULL DEFAULT false")
    op.execute("ALTER TABLE walker_profiles ADD COLUMN IF NOT EXISTS verified_at TIMESTAMP WITHOUT TIME ZONE")
    op.execute("ALTER TABLE walker_profiles ADD COLUMN IF NOT EXISTS verified_by_admin_id VARCHAR")


def downgrade() -> None:
    op.execute("ALTER TABLE walker_profiles DROP COLUMN IF EXISTS verified_by_admin_id")
    op.execute("ALTER TABLE walker_profiles DROP COLUMN IF EXISTS verified_at")
    op.execute("ALTER TABLE walker_profiles DROP COLUMN IF EXISTS verified")
