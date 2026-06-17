"""Background Check Fase 0 — certidoes de antecedentes do passeador.

Cria a tabela walker_background_certificates (1 linha por certidao) e adiciona
os campos agregados em walker_profiles (status + consentimento LGPD).

ADITIVA e IDEMPOTENTE (has_table / _has_column). Tudo com server_default seguro
("none" p/ status) => ZERO regressao; o efeito so aparece quando a flag de tenant
`background_checks` for ligada (default-OFF).

Revision ID: 0038_walker_background_check
Revises: 0037_user_token_version
Create Date: 2026-06-17
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0038_walker_background_check"
down_revision: Union[str, None] = "0037_user_token_version"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PROFILE_TABLE = "walker_profiles"
_CERT_TABLE = "walker_background_certificates"

_PROFILE_COLUMNS = (
    ("background_check_status", lambda: sa.Column("background_check_status", sa.String(), nullable=False, server_default="none")),
    ("background_verified_at", lambda: sa.Column("background_verified_at", sa.DateTime(), nullable=True)),
    ("background_consent_at", lambda: sa.Column("background_consent_at", sa.DateTime(), nullable=True)),
    ("background_consent_version", lambda: sa.Column("background_consent_version", sa.String(), nullable=True)),
)


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if not _has_table(_CERT_TABLE):
        op.create_table(
            _CERT_TABLE,
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("walker_profile_id", sa.String(), sa.ForeignKey("walker_profiles.id"), nullable=False),
            sa.Column("cert_type", sa.String(), nullable=False),
            sa.Column("issuer_uf", sa.String(), nullable=True),
            sa.Column("document_url", sa.String(), nullable=True),
            sa.Column("cert_number", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=False, server_default="pending"),
            sa.Column("validated_by_admin_id", sa.String(), nullable=True),
            sa.Column("validated_at", sa.DateTime(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.create_index(
            "ix_walker_background_certificates_walker_profile_id",
            _CERT_TABLE,
            ["walker_profile_id"],
        )

    for column_name, column_factory in _PROFILE_COLUMNS:
        if not _has_column(_PROFILE_TABLE, column_name):
            op.add_column(_PROFILE_TABLE, column_factory())


def downgrade() -> None:
    for column_name, _ in _PROFILE_COLUMNS:
        if _has_column(_PROFILE_TABLE, column_name):
            op.drop_column(_PROFILE_TABLE, column_name)
    if _has_table(_CERT_TABLE):
        op.drop_index("ix_walker_background_certificates_walker_profile_id", table_name=_CERT_TABLE)
        op.drop_table(_CERT_TABLE)
