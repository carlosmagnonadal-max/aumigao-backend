"""Fase 3 T1 — comissao protegida, margem do tenant, AppSetting per-tenant.

tenant_payment_configs: adiciona tenant_margin_percent (float, default 0.0).
app_settings: adiciona tenant_id (VARCHAR nullable, index) +
              unique constraint composto (tenant_id, key).

Revision ID: 0023_split_fields
Revises: 0022_invoice_url_wallet
Create Date: 2026-06-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0023_split_fields"
down_revision: Union[str, None] = "0022_invoice_url_wallet"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_column(table: str, column: str) -> bool:
    cols = {c["name"] for c in _inspector().get_columns(table)}
    return column in cols


def _has_index(table: str, index_name: str) -> bool:
    indexes = {i["name"] for i in _inspector().get_indexes(table)}
    return index_name in indexes


def _has_unique_constraint(table: str, constraint_name: str) -> bool:
    try:
        constraints = {uc["name"] for uc in _inspector().get_unique_constraints(table)}
        return constraint_name in constraints
    except Exception:
        return False


def upgrade() -> None:
    # --- tenant_payment_configs: adiciona tenant_margin_percent ---
    if not _has_column("tenant_payment_configs", "tenant_margin_percent"):
        op.add_column(
            "tenant_payment_configs",
            sa.Column(
                "tenant_margin_percent",
                sa.Float(),
                nullable=False,
                server_default="0",
            ),
        )

    # --- app_settings: adiciona id (UUID PK), tenant_id ---
    # A tabela original usa key como PK. A nova estrutura usa id (UUID) como PK
    # e (tenant_id, key) como unique constraint para suporte per-tenant.
    # Em producao: recria a tabela preservando dados (SQLite nao suporta ALTER COLUMN).
    bind = op.get_bind()
    insp = _inspector()
    existing_cols = {c["name"] for c in insp.get_columns("app_settings")}

    needs_id = "id" not in existing_cols
    needs_tenant_id = "tenant_id" not in existing_cols

    if needs_id or needs_tenant_id:
        # Recria a tabela com a nova estrutura, preservando dados existentes.
        # Dados existentes: key e PK, tenant_id NULL (global), value_json, updated_at, updated_by.
        try:
            bind.execute(sa.text("ALTER TABLE app_settings RENAME TO app_settings_old"))
        except Exception:
            pass  # Se falhar (ex: tabela nao existe), pula

        try:
            op.create_table(
                "app_settings",
                sa.Column("id", sa.String(), nullable=False, primary_key=True),
                sa.Column("key", sa.String(), nullable=False),
                sa.Column("tenant_id", sa.String(), nullable=True),
                sa.Column("value_json", sa.Text(), nullable=True),
                sa.Column("updated_at", sa.DateTime(), nullable=True),
                sa.Column("updated_by", sa.String(), nullable=True),
                sa.UniqueConstraint("tenant_id", "key", name="uq_app_settings_tenant_key"),
            )
        except Exception:
            pass

        # Migra dados da tabela antiga
        try:
            bind.execute(sa.text(
                "INSERT INTO app_settings (id, key, tenant_id, value_json, updated_at, updated_by) "
                "SELECT key, key, NULL, value_json, updated_at, updated_by FROM app_settings_old"
            ))
            bind.execute(sa.text("DROP TABLE app_settings_old"))
        except Exception:
            pass
    else:
        # Tabela ja tem as colunas novas — so garante indices
        pass

    # Indice em tenant_id para lookups rapidos
    if not _has_index("app_settings", "ix_app_settings_tenant_id"):
        try:
            op.create_index("ix_app_settings_tenant_id", "app_settings", ["tenant_id"])
        except Exception:
            pass

    # Indice em key para lookups rapidos
    if not _has_index("app_settings", "ix_app_settings_key"):
        try:
            op.create_index("ix_app_settings_key", "app_settings", ["key"])
        except Exception:
            pass


def downgrade() -> None:
    # Remove indices de app_settings
    for idx in ("uq_app_settings_tenant_key", "ix_app_settings_tenant_id", "ix_app_settings_key"):
        try:
            op.drop_index(idx, table_name="app_settings")
        except Exception:
            pass

    # Reverte app_settings para estrutura original (key como PK, sem tenant_id, sem id)
    bind = op.get_bind()
    insp = _inspector()
    existing_cols = {c["name"] for c in insp.get_columns("app_settings")}
    if "id" in existing_cols:
        try:
            bind.execute(sa.text("ALTER TABLE app_settings RENAME TO app_settings_new"))
            op.create_table(
                "app_settings",
                sa.Column("key", sa.String(), nullable=False, primary_key=True),
                sa.Column("value_json", sa.Text(), nullable=True),
                sa.Column("updated_at", sa.DateTime(), nullable=True),
                sa.Column("updated_by", sa.String(), nullable=True),
            )
            # Migra apenas registros globais (tenant_id NULL)
            bind.execute(sa.text(
                "INSERT INTO app_settings (key, value_json, updated_at, updated_by) "
                "SELECT key, value_json, updated_at, updated_by FROM app_settings_new WHERE tenant_id IS NULL"
            ))
            bind.execute(sa.text("DROP TABLE app_settings_new"))
        except Exception:
            pass

    # Remove tenant_margin_percent de tenant_payment_configs
    insp2 = _inspector()
    if "tenant_margin_percent" in {c["name"] for c in insp2.get_columns("tenant_payment_configs")}:
        op.drop_column("tenant_payment_configs", "tenant_margin_percent")
