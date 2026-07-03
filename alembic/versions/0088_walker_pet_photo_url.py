"""selfie obrigatoria: separa foto-com-o-pet (opcional) da selfie do documento.

Adiciona a coluna pet_photo_url ao walker_profiles. Ate agora a foto opcional
"Foto com seu pet" era enviada como tipo "selfie" e caia em selfie_url,
misturando os dois conceitos. A partir daqui:
  - selfie_url    = selfie segurando o documento (obrigatoria no cadastro novo);
  - pet_photo_url = foto com o pet (opcional, tipo de upload dedicado "pet_photo").

ADITIVA e IDEMPOTENTE (has_column). Coluna nullable => ZERO regressao; o fallback
legado (app antigo mandando petPhoto como "selfie") continua em auth.py.

Coluna nova em tabela ja existente (walker_profiles) NAO exige policy RLS nova.

Revision ID: 0088_walker_pet_photo_url
Revises: 0087_pet_self_walks
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0088_walker_pet_photo_url"
down_revision: Union[str, None] = "0087_pet_self_walks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "walker_profiles"
_COLUMN = "pet_photo_url"


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if not _has_column(_TABLE, _COLUMN):
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(), nullable=True))


def downgrade() -> None:
    if _has_column(_TABLE, _COLUMN):
        op.drop_column(_TABLE, _COLUMN)
