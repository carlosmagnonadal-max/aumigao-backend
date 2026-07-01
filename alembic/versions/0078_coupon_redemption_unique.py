"""coupon_redemption_unique: defesa de banco contra double-grant do MESMO usuário
no mesmo cupom quando o cupom é de uso único por usuário (P1 money-fix cupom race).

Contexto: o serviço valida `max_uses_per_user` por CONTAGEM (racy sob concorrência).
O UPDATE atômico do contador total (`uses_count`) já fecha a race do teto global;
esta migration fecha a race POR USUÁRIO adicionando uma coluna denormalizada
`single_use_per_user` no resgate (copiada do cupom no momento do resgate) e um
índice único PARCIAL em (coupon_id, user_id) WHERE single_use_per_user.

Assim, cupons que permitem >1 uso por usuário (single_use_per_user = false) NÃO
são afetados pelo índice, e cupons de uso único (o default, max_uses_per_user=1)
ganham a garantia de banco: dois resgates concorrentes do mesmo user → o segundo
viola o índice e é rejeitado (o serviço captura IntegrityError → 409).

SQLite (testes) suporta índice único parcial (WHERE); então criamos em ambos os
dialetos. No PostgreSQL o padrão RLS já cobre a tabela (migration anterior).

Revision ID: 0078_coupon_redemption_unique
Revises: 0077_rls_growth_loops_tables
"""
from alembic import op
import sqlalchemy as sa

revision = "0078_coupon_redemption_unique"
down_revision = "0077_rls_growth_loops_tables"
branch_labels = None
depends_on = None

_INDEX_NAME = "uq_coupon_redemptions_single_use_per_user"


def upgrade() -> None:
    op.add_column(
        "coupon_redemptions",
        sa.Column(
            "single_use_per_user",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    # Índice único PARCIAL: só vale para resgates de cupons de uso único por usuário.
    op.create_index(
        _INDEX_NAME,
        "coupon_redemptions",
        ["coupon_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("single_use_per_user"),
        sqlite_where=sa.text("single_use_per_user"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="coupon_redemptions")
    op.drop_column("coupon_redemptions", "single_use_per_user")
