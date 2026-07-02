"""tenant_free_plan_trial: colunas de reverse trial do plano free no tenant.

Plano `free` ("Começar"): R$0/mês, comissão própria 20%, REDE desligada, sem
multiplicadores, cap de 40 passeios próprios/mês. Reverse trial: tenant novo entra
como Pro por 21 dias e depois é rebaixado para free.

Colunas ADITIVAS em `tenants` (sem tabela nova → sem RLS nova):
  - trial_ends_at        TIMESTAMP NULL  → fim do reverse trial (Pro completo até lá).
  - trial_downgraded_at  TIMESTAMP NULL  → carimbo do rebaixamento efetivo (idempotência
                                            + notificação uma única vez).

Zero-regressão: NULL para todos os tenants existentes (pro/enterprise) → sem trial,
comportamento idêntico. Só o novo fluxo de criação de tenant free preenche trial_ends_at.

Revision ID: 0085_tenant_free_plan_trial
Revises: 0084_walker_sanctions_check
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0085_tenant_free_plan_trial"
down_revision = "0084_walker_sanctions_check"
branch_labels = None
depends_on = None

_COLUMNS = ("trial_ends_at", "trial_downgraded_at")


def _existing_columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    existing = _existing_columns("tenants")
    for col in _COLUMNS:
        if col not in existing:
            op.add_column("tenants", sa.Column(col, sa.DateTime(), nullable=True))


def downgrade() -> None:
    existing = _existing_columns("tenants")
    for col in reversed(_COLUMNS):
        if col in existing:
            op.drop_column("tenants", col)
