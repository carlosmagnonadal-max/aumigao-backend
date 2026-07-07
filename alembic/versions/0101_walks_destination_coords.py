"""0101 — coordenadas do destino do Pet Tour em walks.

O Pet Tour já grava o destino em texto livre (`walks.destination`, TEXT).
Esta migration adiciona o par de coordenadas escolhido no mapa pelo tutor
(mesma UX do ponto de encontro da 0100):
  - destination_lat: latitude do pino (-90 a 90, nullable).
  - destination_lng: longitude do pino (-180 a 180, nullable).

Campos nullable: passeios standard e Pet Tours agendados antes do mapa
(ou com flag meeting_point_map OFF) ficam só com o texto. Quando
preenchidos, o app do passeador renderiza mapa read-only com pino no
destino + botão "Como chegar" (mesmo componente do ponto de encontro).

RLS: a tabela `walks` já tem a policy `tenant_isolation` da 0043. As 2
colunas novas herdam o mesmo escopo — nenhuma policy é alterada.

PG-only; NO-OP em SQLite (CI/testes). Idempotente (IF NOT EXISTS via
inspect).

Revision ID: 0101_walks_destination_coords
Revises: 0100_walks_meeting_point
Create Date: 2026-07-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0101_walks_destination_coords"
down_revision: Union[str, None] = "0100_walks_meeting_point"
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
    if not _has_column("walks", "destination_lat"):
        op.add_column("walks", sa.Column("destination_lat", sa.Float(), nullable=True))
    if not _has_column("walks", "destination_lng"):
        op.add_column("walks", sa.Column("destination_lng", sa.Float(), nullable=True))


def downgrade() -> None:
    if not _has_table("walks"):
        return
    for col in ("destination_lng", "destination_lat"):
        if _has_column("walks", col):
            op.drop_column("walks", col)
