"""walk_share_links: token publico de passeio ao vivo

Revision ID: 0069_walk_share_links
Revises: 0068_credit_ledger_cycle_reference
"""
from alembic import op
import sqlalchemy as sa

revision = "0069_walk_share_links"
down_revision = "0068_credit_ledger_cycle_reference"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "walk_share_links",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("walk_id", sa.String(), sa.ForeignKey("walks.id"), nullable=False),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("created_by", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_walk_share_links_token", "walk_share_links", ["token"], unique=True)
    op.create_index("ix_walk_share_links_walk_id", "walk_share_links", ["walk_id"])


def downgrade() -> None:
    op.drop_index("ix_walk_share_links_walk_id", table_name="walk_share_links")
    op.drop_index("ix_walk_share_links_token", table_name="walk_share_links")
    op.drop_table("walk_share_links")
