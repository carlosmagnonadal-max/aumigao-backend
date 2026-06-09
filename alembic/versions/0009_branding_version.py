"""Sprint 12 — published_version em tenant_branding (cache de branding, spec §9.4)

Aditivo: coluna com default 1. Reversível.

Revision ID: 0009_branding_version
Revises: 0008_audit_logs
Create Date: 2026-06-09
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0009_branding_version"
down_revision: Union[str, None] = "0008_audit_logs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE tenant_branding ADD COLUMN IF NOT EXISTS published_version INTEGER DEFAULT 1"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tenant_branding DROP COLUMN IF EXISTS published_version")
