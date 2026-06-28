"""Ledger contábil do ciclo de crédito de assinatura (Item 4 — CPC 47).

CAMADA CONTÁBIL PURA: NÃO move dinheiro, NÃO altera saldos, NÃO interfere no
consumo/concessão de créditos. Registra os eventos contábeis do ciclo para:
  - Passivo de crédito (receita diferida) na venda do crédito
  - Reconhecimento de receita no consumo do crédito
  - Reconhecimento de receita de breakage na expiração

Cada chamada é best-effort (try/except no caller) — falha no ledger NUNCA pode
quebrar o fluxo de pagamento/crédito/passeio.

TODO: O tratamento contábil EXATO (momento de reconhecimento de PIS/COFINS,
proporcionalidade do breakage, base de cálculo do passivo) PRECISA DE VALIDAÇÃO
DO CONTADOR antes de ser usado como base de escrituração fiscal. Esta tabela é
uma ESTIMATIVA, não verdade fiscal.
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import Column, DateTime, Float, Integer, String
from sqlalchemy.sql import func

from app.core.database import Base


def _uuid() -> str:
    return str(uuid4())


# Tipos de evento do ledger
LEDGER_LIABILITY_CREATED = "liability_created"      # venda do plano → passivo criado
LEDGER_REVENUE_RECOGNIZED = "revenue_recognized"    # consumo de crédito (passeio coberto) → receita
LEDGER_BREAKAGE_RECOGNIZED = "breakage_recognized"  # crédito expirado/cancelado → receita de breakage


class CreditLedgerEntry(Base):
    """Registro imutável de um evento contábil do ciclo de crédito.

    Uma linha por evento (liability_created | revenue_recognized | breakage_recognized).
    Idempotência garantida por unique constraint em (subscription_id, event_type, walk_id).
    """

    __tablename__ = "credit_ledger_entries"

    id = Column(String, primary_key=True, default=_uuid)

    # Escopo
    tenant_id = Column(String, nullable=False, index=True)
    subscription_id = Column(String, nullable=False, index=True)  # FK lógica → tutor_subscriptions.id

    # Tipo do evento (liability_created | revenue_recognized | breakage_recognized)
    event_type = Column(String, nullable=False, index=True)

    # Quantidade de créditos envolvidos neste evento
    credits_count = Column(Integer, nullable=False, default=0)

    # Valor unitário do crédito (price / walks_per_cycle no momento da assinatura)
    unit_value = Column(Float, nullable=False, default=0.0)

    # Valor total do evento (credits_count × unit_value)
    total_value = Column(Float, nullable=False, default=0.0)

    # walk_id preenchido apenas para revenue_recognized (consumo de 1 crédito por passeio)
    walk_id = Column(String, nullable=True, index=True)

    # payment_id do pagamento original que gerou o passivo (preenchido no liability_created)
    payment_id = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
