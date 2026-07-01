"""referral_gift_flags: marca cupom e passeio como brinde de indicacao

Revision ID: 0071_referral_gift_flags
Revises: 0070_tutor_referral_foundation
"""
from alembic import op
import sqlalchemy as sa

revision = "0071_referral_gift_flags"
down_revision = "0070_tutor_referral_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("coupons", sa.Column("is_referral_gift", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("walks", sa.Column("is_referral_gift", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("walks", "is_referral_gift")
    op.drop_column("coupons", "is_referral_gift")
