"""baseline — schema pré-existente do Aumigão (no-op)

Esta migration é intencionalmente VAZIA. O banco de produção (Neon) já foi
criado por DDL manual antes da introdução do Alembic. O baseline serve apenas
para marcar o ponto de partida do versionamento:

    alembic stamp 0001_baseline   # registra o baseline SEM rodar DDL

A partir daqui, toda alteração de schema é uma migration incremental e
reversível (ver protocolo de não-quebra em docs/RECONCILIACAO-SPEC-CODIGO.md §8).

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-09
"""
from typing import Sequence, Union

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401


revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Baseline: nada a aplicar. O schema já existe no banco.
    pass


def downgrade() -> None:
    # Baseline não é reversível (representa o estado inicial).
    pass
