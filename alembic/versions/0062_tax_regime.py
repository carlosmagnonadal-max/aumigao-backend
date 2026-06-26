"""tax_regime: substitui simples_nacional (bool) por campo de regime em lista

Revision ID: 0062_tax_regime
Revises: 0061_fiscal_provisioning
"""
import sqlalchemy as sa
from alembic import op

revision = "0062_tax_regime"
down_revision = "0061_fiscal_provisioning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_fiscal_config",
        sa.Column("tax_regime", sa.String(), nullable=True),
    )
    # backfill: linhas que tinham simples_nacional=TRUE viram 'simples_nacional'
    op.execute(
        "UPDATE tenant_fiscal_config SET tax_regime = 'simples_nacional' WHERE simples_nacional IS TRUE"
    )
    op.drop_column("tenant_fiscal_config", "simples_nacional")


def downgrade() -> None:
    op.add_column(
        "tenant_fiscal_config",
        sa.Column("simples_nacional", sa.Boolean(), nullable=True),
    )
    # backfill inverso: tax_regime='simples_nacional' vira simples_nacional=TRUE
    op.execute(
        "UPDATE tenant_fiscal_config SET simples_nacional = TRUE WHERE tax_regime = 'simples_nacional'"
    )
    op.drop_column("tenant_fiscal_config", "tax_regime")
