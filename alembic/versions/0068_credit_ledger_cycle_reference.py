"""credit_ledger_entries: adiciona cycle_reference para idempotência por ciclo (P1)

P1 (CPC 47 §106): cada renovação mensal de assinatura é uma nova venda de
créditos e deve gerar um novo passivo de contrato. A coluna cycle_reference
(YYYY-MM-DD de current_period_start) torna a chave de idempotência do
liability_created dependente do ciclo, e não apenas da subscription.

O índice único parcial uq_credit_ledger_liability_per_sub (1 liability por
subscription) é substituído por uq_credit_ledger_liability_per_cycle
(1 liability por subscription × ciclo), permitindo múltiplas renovações.

Revision ID: 0068_credit_ledger_cycle_reference
Revises: 0067_credit_ledger
"""
import sqlalchemy as sa
from alembic import op

revision = "0068_credit_ledger_cycle_reference"
down_revision = "0067_credit_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Adiciona a coluna cycle_reference
    op.add_column(
        "credit_ledger_entries",
        sa.Column("cycle_reference", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_credit_ledger_cycle_reference",
        "credit_ledger_entries",
        ["cycle_reference"],
    )

    # 2. Backfill idempotente: preenche cycle_reference das linhas liability_created
    #    usando current_period_start da assinatura no momento da escrita.
    #    A cláusula AND e.cycle_reference IS NULL garante re-run seguro.
    op.execute(
        """
        UPDATE credit_ledger_entries AS e
        SET cycle_reference = to_char(s.current_period_start, 'YYYY-MM-DD')
        FROM tutor_subscriptions AS s
        WHERE e.subscription_id = s.id
          AND e.event_type = 'liability_created'
          AND e.cycle_reference IS NULL
        """
    )

    # 3. Troca o índice único de liability: por subscription → por (subscription, ciclo)
    op.drop_index("uq_credit_ledger_liability_per_sub", table_name="credit_ledger_entries")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_credit_ledger_liability_per_cycle
        ON credit_ledger_entries (subscription_id, cycle_reference)
        WHERE event_type = 'liability_created'
        """
    )
    # Nota: uq_credit_ledger_breakage_per_sub e uq_credit_ledger_revenue_per_walk
    # não são alterados — suas semânticas de idempotência permanecem as mesmas.


def downgrade() -> None:
    # Reverte: remove índice por ciclo, recria índice por subscription, remove coluna
    op.execute("DROP INDEX IF EXISTS uq_credit_ledger_liability_per_cycle")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_credit_ledger_liability_per_sub
        ON credit_ledger_entries (subscription_id)
        WHERE event_type = 'liability_created'
        """
    )
    op.drop_index("ix_credit_ledger_cycle_reference", table_name="credit_ledger_entries")
    op.drop_column("credit_ledger_entries", "cycle_reference")
