"""Onda 1 — Pet Tour (follow-up walker): walker_profiles.has_vehicle

Aditivo e reversível. Passeador com carro é requisito para receber Pet Tour
(gating no matching: get_eligible_walkers filtra has_vehicle quando modality=pet_tour).

Revision ID: 0013_walker_has_vehicle
Revises: 0012_pet_tour
Create Date: 2026-06-09
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0013_walker_has_vehicle"
down_revision: Union[str, None] = "0012_pet_tour"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE walker_profiles ADD COLUMN IF NOT EXISTS has_vehicle BOOLEAN NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE walker_profiles DROP COLUMN IF EXISTS has_vehicle")
