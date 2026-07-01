"""tutor_referral_held_credits: creditos de indicacao retidos ate o tutor assinar

Revision ID: 0072_tutor_referral_held_credits
Revises: 0071_referral_gift_flags
"""
from alembic import op
import sqlalchemy as sa

revision = "0072_tutor_referral_held_credits"
down_revision = "0071_referral_gift_flags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tutor_referrals", sa.Column("held_credits_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tutor_referrals", "held_credits_json")
