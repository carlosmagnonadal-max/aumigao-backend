"""tutor subscription credits_granted gate

Revision ID: 0057_tutor_subscription_credits_granted
Revises: 0056_walk_subscription_credit
"""
from alembic import op
import sqlalchemy as sa

revision = "0057_tutor_subscription_credits_granted"
down_revision = "0056_walk_subscription_credit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tutor_subscriptions",
        sa.Column("credits_granted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute("UPDATE tutor_subscriptions SET credits_granted = true")


def downgrade() -> None:
    op.drop_column("tutor_subscriptions", "credits_granted")
