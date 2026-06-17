"""A-02 — Foreign Keys faltantes + ondelete em 8 relacoes criticas.

Adiciona FKs explícitas (com nome e ondelete) nas tabelas payments,
push_tokens, shared_walk_participants e coupon_redemptions. Todas as 8
constraints foram verificadas em producao sem orfaos antes desta migration.

Regras de ondelete escolhidas:
  - RESTRICT : coluna NOT NULL (tutor_id, user_id, pet_id) — rejeita a
               deleção do pai enquanto houver filho.
  - SET NULL  : coluna nullable (walk_id, tenant_id) — desvincula o
               filho quando o pai e removido; nao orphaniza o registro.
  - CASCADE   : push_tokens.user_id — faz sentido semantico (token sem
               usuario nao serve para nada).

Compatibilidade SQLite: usa op.batch_alter_table para que o Alembic
recrie a tabela (SQLite nao suporta ALTER TABLE ADD CONSTRAINT). Em
Postgres o batch gera ALTER TABLE nativo.

Idempotente: inspeciona as FKs existentes antes de criar/dropar.

Revision ID: 0036_fk_integrity
Revises: 0035_payment_indexes
Create Date: 2026-06-17
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa

revision: str = "0036_fk_integrity"
down_revision: Union[str, None] = "0035_payment_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ---------------------------------------------------------------------------
# Definição das 8 FKs
# Cada entrada: (constraint_name, table, local_col, ref_table, ref_col, ondelete)
# ---------------------------------------------------------------------------
_FKS: list[tuple[str, str, str, str, str, str]] = [
    # payments -----------------------------------------------------------
    ("fk_payments_tutor_id_users",   "payments", "tutor_id",   "users",   "id", "RESTRICT"),
    ("fk_payments_walk_id_walks",    "payments", "walk_id",    "walks",   "id", "SET NULL"),
    ("fk_payments_tenant_id_tenants","payments", "tenant_id",  "tenants", "id", "SET NULL"),
    # push_tokens --------------------------------------------------------
    ("fk_push_tokens_user_id_users", "push_tokens", "user_id", "users",   "id", "CASCADE"),
    # shared_walk_participants -------------------------------------------
    ("fk_swp_tutor_id_users",        "shared_walk_participants", "tutor_id", "users", "id", "RESTRICT"),
    ("fk_swp_pet_id_pets",           "shared_walk_participants", "pet_id",   "pets",  "id", "RESTRICT"),
    # coupon_redemptions -------------------------------------------------
    ("fk_coupon_redemptions_user_id_users", "coupon_redemptions", "user_id", "users", "id", "RESTRICT"),
    ("fk_coupon_redemptions_walk_id_walks", "coupon_redemptions", "walk_id", "walks", "id", "SET NULL"),
]


def _existing_fk_names(bind, table: str) -> set[str]:
    """Retorna os nomes de FK constraints já existentes em 'table'.

    Em modo offline (--sql) não há conexão inspecionável: retorna vazio para
    que o script SQL emita todas as constraints (geração de script puro).
    """
    if context.is_offline_mode():
        return set()
    return {fk["name"] for fk in sa.inspect(bind).get_foreign_keys(table)}


def upgrade() -> None:
    bind = op.get_bind()

    # Agrupa FKs por tabela para minimizar o número de batch contexts.
    tables: dict[str, list[tuple]] = {}
    for fk in _FKS:
        tables.setdefault(fk[1], []).append(fk)

    for table, fks in tables.items():
        existing = _existing_fk_names(bind, table)
        # Verifica se há alguma FK nova para esta tabela.
        to_add = [fk for fk in fks if fk[0] not in existing]
        if not to_add:
            continue
        with op.batch_alter_table(table, schema=None) as batch_op:
            for name, _table, local_col, ref_table, ref_col, ondelete in to_add:
                batch_op.create_foreign_key(
                    name,
                    ref_table,
                    [local_col],
                    [ref_col],
                    ondelete=ondelete,
                )


def downgrade() -> None:
    bind = op.get_bind()

    # Agrupa por tabela (mesma lógica do upgrade, ordem inversa).
    tables: dict[str, list[tuple]] = {}
    for fk in _FKS:
        tables.setdefault(fk[1], []).append(fk)

    for table, fks in reversed(list(tables.items())):
        existing = _existing_fk_names(bind, table)
        to_drop = [fk for fk in fks if fk[0] in existing]
        if not to_drop:
            continue
        with op.batch_alter_table(table, schema=None) as batch_op:
            for name, _table, local_col, ref_table, ref_col, ondelete in to_drop:
                batch_op.drop_constraint(name, type_="foreignkey")
