"""Tenant background_check_provider setting (migration 0040).

Adiciona duas colunas a tenant_settings:
- background_check_provider  (String, NOT NULL, server_default='manual')
  Provedor plugavel de background check por tenant.
  Valores validos: "manual" | "flagcheck" | "idwall" | "serpro".
  Default "manual" => ZERO regressao; comportamento identico ao anterior.

- background_check_provider_config  (Text, NULLABLE)
  JSON de credenciais/config do provedor pago.
  TODO: cifrar com Fernet/KMS antes de habilitar provedor pago em producao.
  Nao usada por nenhum tenant hoje; reservada para extensao futura.

ADITIVA e IDEMPOTENTE (_has_table / _has_column). server_default seguro
=> nao quebra linhas existentes em producao.

Revision ID: 0040_bg_check_provider
Revises: 0039_users_must_change_password
Create Date: 2026-06-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0040_bg_check_provider"
down_revision: Union[str, None] = "0039_users_must_change_password"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SETTINGS_TABLE = "tenant_settings"

_NEW_COLUMNS = (
    (
        "background_check_provider",
        lambda: sa.Column(
            "background_check_provider",
            sa.String(),
            nullable=False,
            server_default="manual",
        ),
    ),
    (
        "background_check_provider_config",
        lambda: sa.Column(
            "background_check_provider_config",
            sa.Text(),
            nullable=True,
        ),
    ),
)


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if not _has_table(_SETTINGS_TABLE):
        # tenant_settings nao existe (banco recém-criado sem seed) — nada a fazer.
        return

    for column_name, column_factory in _NEW_COLUMNS:
        if not _has_column(_SETTINGS_TABLE, column_name):
            op.add_column(_SETTINGS_TABLE, column_factory())


def downgrade() -> None:
    if not _has_table(_SETTINGS_TABLE):
        return

    for column_name, _ in _NEW_COLUMNS:
        if _has_column(_SETTINGS_TABLE, column_name):
            op.drop_column(_SETTINGS_TABLE, column_name)
