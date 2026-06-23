from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0050_walker_availability_exceptions"
down_revision: Union[str, None] = "0049_walks_policy_walker_self"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "walker_availability_exceptions" not in insp.get_table_names():
        op.create_table(
            "walker_availability_exceptions",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("walker_user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("exception_date", sa.Date(), nullable=False),
            sa.Column("kind", sa.String(length=8), nullable=False),
            sa.Column("start_time", sa.String(length=5), nullable=True),
            sa.Column("end_time", sa.String(length=5), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.execute("CREATE INDEX IF NOT EXISTS ix_wae_walker_user_id ON walker_availability_exceptions (walker_user_id)")
        op.execute("CREATE INDEX IF NOT EXISTS ix_wae_exception_date ON walker_availability_exceptions (exception_date)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS walker_availability_exceptions")
