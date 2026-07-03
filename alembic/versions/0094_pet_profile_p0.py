"""Perfil Vivo P0 — ficha expandida do pet (registro rápido sem GPS).

Adiciona colunas ADITIVAS e NULL em `pets` (zero default disruptivo):
  - supplements_json     TEXT  — lista JSON de {name, dose, frequency}
  - food_bag_weight_kg   FLOAT — peso da embalagem da ração atual (estimador de recompra P1)
  - food_bag_opened_at   DATE  — quando abriu a embalagem atual
  - vet_clinic           STRING — clínica do veterinário de referência
                                  (vet_name/vet_phone JÁ existem desde a 0073)
  - insurance_provider   STRING — plano de saúde pet (operadora)
  - insurance_policy     STRING — nº da apólice/carteirinha
  - behavior_with_dogs   STRING — amigavel|indiferente|reativo|desconhecido
  - behavior_with_children STRING — idem
  - behavior_with_cats   STRING — idem
  - fear_triggers_json   TEXT  — lista JSON de strings (ex.: trovão, fogos, aspirador)

ADITIVA e IDEMPOTENTE (has_column) — coluna nova em tabela já existente (pets),
NULL => zero regressão e sem backfill. Compatível PG + SQLite (padrão da 0088).

SEM mudança de RLS: a policy `tenant_isolation` de pets (0093) cobre colunas novas
automaticamente; os event_types novos de rotina/saúde SEGUEM o tutor pela mesma 0093
(o guard exclui só walk_observation/tenant_note), então nada aqui toca policy.

Revision ID: 0094_pet_profile_p0
Revises: 0093_rls_pets_follow_tutor
Create Date: 2026-07-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0094_pet_profile_p0"
down_revision: Union[str, None] = "0093_rls_pets_follow_tutor"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "pets"

# (nome, tipo) — ordem de criação. Downgrade dropa na ordem inversa.
_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("supplements_json", sa.Text()),
    ("food_bag_weight_kg", sa.Float()),
    ("food_bag_opened_at", sa.Date()),
    ("vet_clinic", sa.String()),
    ("insurance_provider", sa.String()),
    ("insurance_policy", sa.String()),
    ("behavior_with_dogs", sa.String()),
    ("behavior_with_children", sa.String()),
    ("behavior_with_cats", sa.String()),
    ("fear_triggers_json", sa.Text()),
)


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    for name, coltype in _COLUMNS:
        if not _has_column(_TABLE, name):
            op.add_column(_TABLE, sa.Column(name, coltype, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_COLUMNS):
        if _has_column(_TABLE, name):
            op.drop_column(_TABLE, name)
