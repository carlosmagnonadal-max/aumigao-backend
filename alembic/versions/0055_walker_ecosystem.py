"""walker ecosystem — cr_wallets, cr_transactions, gamification_events, smart_notifications

Revision ID: 0055_walker_ecosystem
Revises: 0054_walker_pix_key
Create Date: 2026-06-25
"""
from __future__ import annotations
from typing import Union
import sqlalchemy as sa
from alembic import op

revision: str = "0055_walker_ecosystem"
down_revision: Union[str, None] = "0054_walker_pix_key"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def upgrade() -> None:
    # -------------------------------------------------------------------------
    # walker_cr_wallets — one wallet per walker, tracks CR balance + lifetime
    # -------------------------------------------------------------------------
    if not _has_table("walker_cr_wallets"):
        op.create_table(
            "walker_cr_wallets",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column(
                "walker_user_id",
                sa.String(),
                sa.ForeignKey("users.id"),
                nullable=False,
            ),
            sa.Column("balance", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("lifetime_earned", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("lifetime_spent", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("walker_user_id", name="uq_walker_cr_wallets_walker_user_id"),
        )
        op.create_index(
            "ix_walker_cr_wallets_walker_user_id",
            "walker_cr_wallets",
            ["walker_user_id"],
        )

    # -------------------------------------------------------------------------
    # walker_cr_transactions — ledger of every CR movement (earn/spend/penalty/admin)
    # -------------------------------------------------------------------------
    if not _has_table("walker_cr_transactions"):
        op.create_table(
            "walker_cr_transactions",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column(
                "walker_user_id",
                sa.String(),
                sa.ForeignKey("users.id"),
                nullable=False,
            ),
            sa.Column("amount", sa.Integer(), nullable=False),
            sa.Column("tx_type", sa.String(), nullable=False),
            sa.Column("source", sa.String(), nullable=False),
            sa.Column("description", sa.Text(), nullable=True, server_default=""),
            sa.Column("related_entity_type", sa.String(), nullable=True),
            sa.Column("related_entity_id", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
        op.create_index(
            "ix_walker_cr_transactions_walker_user_id",
            "walker_cr_transactions",
            ["walker_user_id"],
        )

    # -------------------------------------------------------------------------
    # walker_gamification_events — feed of gamification milestones per walker
    # -------------------------------------------------------------------------
    if not _has_table("walker_gamification_events"):
        op.create_table(
            "walker_gamification_events",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column(
                "walker_user_id",
                sa.String(),
                sa.ForeignKey("users.id"),
                nullable=False,
            ),
            sa.Column("event_type", sa.String(), nullable=False),
            sa.Column("title", sa.String(), nullable=False),
            sa.Column("description", sa.Text(), nullable=True, server_default=""),
            sa.Column("cr_amount", sa.Integer(), nullable=True),
            sa.Column("related_entity_type", sa.String(), nullable=True),
            sa.Column("related_entity_id", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
        op.create_index(
            "ix_walker_gamification_events_walker_user_id",
            "walker_gamification_events",
            ["walker_user_id"],
        )

    # -------------------------------------------------------------------------
    # walker_smart_notifications — smart/triggered notifications for walkers
    # -------------------------------------------------------------------------
    if not _has_table("walker_smart_notifications"):
        op.create_table(
            "walker_smart_notifications",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column(
                "walker_user_id",
                sa.String(),
                sa.ForeignKey("users.id"),
                nullable=False,
            ),
            sa.Column("notification_type", sa.String(), nullable=False),
            sa.Column("title", sa.String(), nullable=False),
            sa.Column("message", sa.Text(), nullable=True, server_default=""),
            sa.Column("priority", sa.String(), nullable=False, server_default="normal"),
            sa.Column("trigger_source", sa.String(), nullable=False),
            sa.Column("read_at", sa.DateTime(), nullable=True),
            sa.Column("sent_at", sa.DateTime(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
        op.create_index(
            "ix_walker_smart_notifications_walker_user_id",
            "walker_smart_notifications",
            ["walker_user_id"],
        )


def downgrade() -> None:
    if _has_table("walker_smart_notifications"):
        op.drop_table("walker_smart_notifications")
    if _has_table("walker_gamification_events"):
        op.drop_table("walker_gamification_events")
    if _has_table("walker_cr_transactions"):
        op.drop_table("walker_cr_transactions")
    if _has_table("walker_cr_wallets"):
        op.drop_table("walker_cr_wallets")
