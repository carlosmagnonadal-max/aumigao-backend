"""credit_ledger_entries: ledger contábil do ciclo de crédito (CPC 47)

Cria a tabela credit_ledger_entries para registrar eventos contábeis do ciclo de
crédito de assinatura: passivo (liability_created), reconhecimento de receita
(revenue_recognized) e breakage (breakage_recognized).

CAMADA CONTÁBIL PURA — não move dinheiro, não altera saldos.
Gated por CREDIT_LEDGER_ENABLED (default ON).

TODO: Tratamento fiscal exato (PIS/COFINS, breakage proporcional) precisa de
validação do contador antes de usar como base de escrituração.

Revision ID: 0067_credit_ledger
Revises: 0066_walker_profile_suspension_audit
"""
import sqlalchemy as sa
from alembic import op

revision = "0067_credit_ledger"
down_revision = "0066_walker_profile_suspension_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "credit_ledger_entries",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("subscription_id", sa.String(), nullable=False),
        # event_type: liability_created | revenue_recognized | breakage_recognized
        sa.Column("event_type", sa.String(), nullable=False),
        # créditos envolvidos neste evento
        sa.Column("credits_count", sa.Integer(), nullable=False, server_default="0"),
        # valor unitário (price / walks_per_cycle no snapshot da assinatura)
        sa.Column("unit_value", sa.Numeric(10, 4), nullable=False, server_default="0"),
        # valor total = credits_count × unit_value
        sa.Column("total_value", sa.Numeric(10, 2), nullable=False, server_default="0"),
        # preenchido apenas em revenue_recognized (passeio que consumiu o crédito)
        sa.Column("walk_id", sa.String(), nullable=True),
        # preenchido em liability_created (pagamento que originou o passivo)
        sa.Column("payment_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    # Índices de consulta
    op.create_index("ix_credit_ledger_tenant_id", "credit_ledger_entries", ["tenant_id"])
    op.create_index("ix_credit_ledger_subscription_id", "credit_ledger_entries", ["subscription_id"])
    op.create_index("ix_credit_ledger_event_type", "credit_ledger_entries", ["event_type"])
    op.create_index("ix_credit_ledger_walk_id", "credit_ledger_entries", ["walk_id"])
    # Idempotência: 1 breakage por subscription; 1 revenue por (subscription, walk)
    # Partial unique index para breakage_recognized (1 por subscription)
    op.execute(
        """
        CREATE UNIQUE INDEX uq_credit_ledger_breakage_per_sub
        ON credit_ledger_entries (subscription_id)
        WHERE event_type = 'breakage_recognized'
        """
    )
    # Partial unique index para revenue_recognized por walk (1 por passeio)
    op.execute(
        """
        CREATE UNIQUE INDEX uq_credit_ledger_revenue_per_walk
        ON credit_ledger_entries (walk_id)
        WHERE event_type = 'revenue_recognized'
        """
    )
    # Partial unique index para liability_created (1 por subscription)
    op.execute(
        """
        CREATE UNIQUE INDEX uq_credit_ledger_liability_per_sub
        ON credit_ledger_entries (subscription_id)
        WHERE event_type = 'liability_created'
        """
    )


def downgrade() -> None:
    op.drop_index("uq_credit_ledger_liability_per_sub", table_name="credit_ledger_entries")
    op.drop_index("uq_credit_ledger_revenue_per_walk", table_name="credit_ledger_entries")
    op.drop_index("uq_credit_ledger_breakage_per_sub", table_name="credit_ledger_entries")
    op.drop_index("ix_credit_ledger_walk_id", table_name="credit_ledger_entries")
    op.drop_index("ix_credit_ledger_event_type", table_name="credit_ledger_entries")
    op.drop_index("ix_credit_ledger_subscription_id", table_name="credit_ledger_entries")
    op.drop_index("ix_credit_ledger_tenant_id", table_name="credit_ledger_entries")
    op.drop_table("credit_ledger_entries")
