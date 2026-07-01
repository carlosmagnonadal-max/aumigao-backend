"""tutor_referral_foundation: config por tenant + rastreio de indicacao do tutor

Revision ID: 0070_tutor_referral_foundation
Revises: 0069_walk_share_links
"""
from alembic import op
import sqlalchemy as sa

revision = "0070_tutor_referral_foundation"
down_revision = "0069_walk_share_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tutor_referral_configs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reward_type", sa.String(), nullable=False, server_default="desconto"),
        sa.Column("discount_kind", sa.String(), nullable=False, server_default="percent"),
        sa.Column("discount_value", sa.Float(), nullable=False, server_default="0"),
        sa.Column("free_walks_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("credit_walks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("same_reward_both_sides", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("referrer_multiplier", sa.Float(), nullable=False, server_default="1"),
        sa.Column("referred_multiplier", sa.Float(), nullable=False, server_default="1"),
        sa.Column("trigger_type", sa.String(), nullable=False, server_default="primeiro_passeio_pago"),
        sa.Column("trigger_n", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_tutor_referral_configs_tenant_id", "tutor_referral_configs", ["tenant_id"], unique=True
    )

    op.create_table(
        "tutor_referrals",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("referrer_user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("referred_user_id", sa.String(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("referral_code", sa.String(), nullable=False),
        sa.Column("invite_link", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("reward_status", sa.String(), nullable=False, server_default="not_eligible"),
        sa.Column("reward_snapshot_json", sa.Text(), nullable=True),
        sa.Column("completed_paid_walks_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("converted_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("tenant_id", "referred_user_id", name="uq_tutor_referral_tenant_referred"),
    )
    op.create_index("ix_tutor_referrals_tenant_id", "tutor_referrals", ["tenant_id"])
    op.create_index("ix_tutor_referrals_referrer_user_id", "tutor_referrals", ["referrer_user_id"])
    op.create_index("ix_tutor_referrals_referred_user_id", "tutor_referrals", ["referred_user_id"])
    op.create_index("ix_tutor_referrals_referral_code", "tutor_referrals", ["referral_code"], unique=True)
    op.create_index("ix_tutor_referrals_status", "tutor_referrals", ["status"])
    op.create_index("ix_tutor_referrals_reward_status", "tutor_referrals", ["reward_status"])


def downgrade() -> None:
    for idx in (
        "ix_tutor_referrals_reward_status", "ix_tutor_referrals_status",
        "ix_tutor_referrals_referral_code", "ix_tutor_referrals_referred_user_id",
        "ix_tutor_referrals_referrer_user_id", "ix_tutor_referrals_tenant_id",
    ):
        op.drop_index(idx, table_name="tutor_referrals")
    op.drop_table("tutor_referrals")
    op.drop_index("ix_tutor_referral_configs_tenant_id", table_name="tutor_referral_configs")
    op.drop_table("tutor_referral_configs")
