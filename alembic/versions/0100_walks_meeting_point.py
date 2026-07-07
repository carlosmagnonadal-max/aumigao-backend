"""0100 — ponto de encontro dedicado em walks.

Substitui o hack existente (`notes` com prefixo "Ponto de encontro: X") por
3 colunas estruturadas:
  - meeting_point: endereço em texto livre (até 500 chars).
  - meeting_lat:  latitude do pino (-90 a 90, nullable).
  - meeting_lng:  longitude do pino (-180 a 180, nullable).

Campos nullable: o passeio "buscar em casa" (default) não preenche esses
campos. Quando preenchidos, o app do passeador renderiza mapa read-only com
pino no local + botão "Como chegar" via Linking nativo (iOS Apple Maps,
Android Google Maps).

RLS: a tabela `walks` já tem a policy `tenant_isolation` da 0043. Esta
migration NÃO toca em policy — a leitura/escrita continua escopada pelo
tenant do request. As 3 colunas novas herdam o mesmo escopo.

PG-only; NO-OP em SQLite (CI/testes). Idempotente (IF NOT EXISTS via
inspect).

Revision ID: 0100_walks_meeting_point
Revises: 0099_rls_support_tickets_user_own
Create Date: 2026-07-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0100_walks_meeting_point"
down_revision: Union[str, None] = "0099_rls_support_tickets_user_own"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return _inspector().has_table(name)


def _has_column(table: str, column: str) -> bool:
    if not _has_table(table):
        return False
    existing = {c["name"] for c in _inspector().get_columns(table)}
    return column in existing


def upgrade() -> None:
    if not _has_table("walks"):
        return
    if not _has_column("walks", "meeting_point"):
        op.add_column("walks", sa.Column("meeting_point", sa.String(500), nullable=True))
    if not _has_column("walks", "meeting_lat"):
        op.add_column("walks", sa.Column("meeting_lat", sa.Float(), nullable=True))
    if not _has_column("walks", "meeting_lng"):
        op.add_column("walks", sa.Column("meeting_lng", sa.Float(), nullable=True))


def downgrade() -> None:
    if not _has_table("walks"):
        return
    conn = op.get_bind()
    for col in ("meeting_lng", "meeting_lat", "meeting_point"):
        if _has_column("walks", col):
            op.drop_column("walks", col)
