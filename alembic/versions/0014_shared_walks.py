"""Onda 1 — passeios compartilhados: config por tenant + sessões + participantes

Aditivo e reversível. Convite primeiro; pool atrás de toggle (pool_enabled, off).

Revision ID: 0014_shared_walks
Revises: 0013_walker_has_vehicle
Create Date: 2026-06-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0014_shared_walks"
down_revision: Union[str, None] = "0013_walker_has_vehicle"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tenant_shared_walk_configs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("price_per_pet", sa.Float(), nullable=False, server_default="29.90"),
        sa.Column("max_pets_same_tutor", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("max_tutors", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("pool_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pool_radius_km", sa.Float(), nullable=False, server_default="3.0"),
        sa.Column("pool_time_window_min", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id"),
    )
    op.create_index("ix_tenant_shared_walk_configs_tenant_id", "tenant_shared_walk_configs", ["tenant_id"])

    op.create_table(
        "shared_walks",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("created_by_tutor_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="forming"),
        sa.Column("origin", sa.String(), nullable=False, server_default="invite"),
        sa.Column("scheduled_date", sa.String(), server_default=""),
        sa.Column("duration_minutes", sa.Integer(), server_default="45"),
        sa.Column("price_per_pet", sa.Float(), server_default="0"),
        sa.Column("max_tutors", sa.Integer(), server_default="2"),
        sa.Column("open_to_pool", sa.Boolean(), server_default=sa.false()),
        sa.Column("walker_id", sa.String(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_shared_walks_tenant_id", "shared_walks", ["tenant_id"])
    op.create_index("ix_shared_walks_created_by_tutor_id", "shared_walks", ["created_by_tutor_id"])
    op.create_index("ix_shared_walks_status", "shared_walks", ["status"])

    op.create_table(
        "shared_walk_participants",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("shared_walk_id", sa.String(), nullable=False),
        sa.Column("tutor_id", sa.String(), nullable=False),
        sa.Column("pet_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), server_default="guest"),
        sa.Column("status", sa.String(), nullable=False, server_default="invited"),
        sa.Column("price", sa.Float(), server_default="0"),
        sa.Column("payment_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["shared_walk_id"], ["shared_walks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_shared_walk_participants_shared_walk_id", "shared_walk_participants", ["shared_walk_id"])
    op.create_index("ix_shared_walk_participants_tutor_id", "shared_walk_participants", ["tutor_id"])
    op.create_index("ix_shared_walk_participants_status", "shared_walk_participants", ["status"])


def downgrade() -> None:
    op.drop_table("shared_walk_participants")
    op.drop_index("ix_shared_walks_status", table_name="shared_walks")
    op.drop_index("ix_shared_walks_created_by_tutor_id", table_name="shared_walks")
    op.drop_index("ix_shared_walks_tenant_id", table_name="shared_walks")
    op.drop_table("shared_walks")
    op.drop_index("ix_tenant_shared_walk_configs_tenant_id", table_name="tenant_shared_walk_configs")
    op.drop_table("tenant_shared_walk_configs")
