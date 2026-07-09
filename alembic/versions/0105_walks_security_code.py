"""walks.security_code — Código de Coleta (prova de entrega presencial).

Backfill: todo walk existente ganha um código (mesmo finalizado — inofensivo),
para que passeios ativos do teste real já tenham código ao vivo.
"""
from alembic import op
import sqlalchemy as sa

revision = "0105_walks_security_code"
down_revision = "0104_walks_experience"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("walks", sa.Column("security_code", sa.String(length=4), nullable=True))
    # Backfill server-side (Postgres): 4 dígitos aleatórios com zeros à esquerda.
    op.execute(
        "UPDATE walks SET security_code = lpad(floor(random() * 10000)::int::text, 4, '0') "
        "WHERE security_code IS NULL"
    )


def downgrade() -> None:
    op.drop_column("walks", "security_code")
