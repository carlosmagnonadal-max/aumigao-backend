"""walk subscription coverage + credit refund flag

Revision ID: 0056_walk_subscription_credit
Revises: 0055_walker_ecosystem
"""
from alembic import op
import sqlalchemy as sa

revision = "0056_walk_subscription_credit"
down_revision = "0055_walker_ecosystem"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("walks", sa.Column("subscription_id", sa.String(), nullable=True))
    op.add_column(
        "walks",
        sa.Column("credit_refunded", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_walks_subscription_id", "walks", ["subscription_id"])
    op.create_foreign_key(
        "fk_walks_subscription_id_tutor_subscriptions",
        "walks",
        "tutor_subscriptions",
        ["subscription_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_walks_subscription_id_tutor_subscriptions", "walks", type_="foreignkey")
    op.drop_index("ix_walks_subscription_id", table_name="walks")
    op.drop_column("walks", "credit_refunded")
    op.drop_column("walks", "subscription_id")
