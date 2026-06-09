"""M5 (passo 2) — backfill de tenant_id a partir das entidades pai

Popula o tenant_id das tabelas tenant-scoped herdando do registro pai:
  - via walk_id  -> walks.tenant_id   (todas as 6 tabelas, quando há walk)
  - fallback via user (payments.tutor_id / complaints.author_id) quando não há walk

Idempotente (só atualiza onde tenant_id IS NULL) e não-destrutivo. Linhas órfãs
(sem walk e sem user com tenant) permanecem com tenant_id NULL — aceitável: o
super_admin continua vendo tudo; o filtro por tenant simplesmente não as atribui.

Downgrade é no-op de propósito: reverter um backfill de dados apagaria também
tenant_id legítimos de registros criados depois. A reversão estrutural (drop da
coluna) vive na migration 0002.

Revision ID: 0003_backfill_tenant_id
Revises: 0002_add_tenant_id_tenant_scoped
Create Date: 2026-06-09
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0003_backfill_tenant_id"
down_revision: Union[str, None] = "0002_add_tenant_id_tenant_scoped"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Tabelas cujo tenant_id pode ser herdado do walk via walk_id.
_VIA_WALK: tuple[str, ...] = (
    "walk_reviews",
    "walker_reviews",
    "walk_tips",
    "walk_completion_reviews",
    "payments",
    "complaints",
)


def upgrade() -> None:
    # 1) Herda de walks via walk_id (cobre as 4 tabelas com walk obrigatório e
    #    as 2 com walk opcional quando o walk existe).
    for table in _VIA_WALK:
        op.execute(
            f"UPDATE {table} SET tenant_id = w.tenant_id "
            f"FROM walks w "
            f"WHERE {table}.walk_id = w.id "
            f"AND {table}.tenant_id IS NULL AND w.tenant_id IS NOT NULL"
        )

    # 2) Fallback via usuário para as tabelas com walk opcional.
    op.execute(
        "UPDATE payments SET tenant_id = u.tenant_id "
        "FROM users u "
        "WHERE payments.tutor_id = u.id "
        "AND payments.tenant_id IS NULL AND u.tenant_id IS NOT NULL"
    )
    op.execute(
        "UPDATE complaints SET tenant_id = u.tenant_id "
        "FROM users u "
        "WHERE complaints.author_id = u.id "
        "AND complaints.tenant_id IS NULL AND u.tenant_id IS NOT NULL"
    )


def downgrade() -> None:
    # No-op intencional (ver docstring). A reversão estrutural está na 0002.
    pass
