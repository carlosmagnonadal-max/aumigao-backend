"""Ledger imutável de comissão medida do tenant (Fase 1).

Uma entrada por passeio finalizado de passeador PRÓPRIO do tenant. Snapshot da
taxa resolvida no momento da finalização (variável: par/tenant/plano). Passeio de
REDE não gera entrada aqui (margem capturada no preço do crédito — Fase 2).
"""
from sqlalchemy import Boolean, Column, Float, String, DateTime, Index
from sqlalchemy.sql import func

from app.core.database import Base

# status do ciclo de cobrança
COMM_ACCRUED = "accrued"   # passeio medido, ainda não faturado
COMM_BILLED = "billed"     # incluído numa cobrança Asaas emitida
COMM_PAID = "paid"         # cobrança paga pelo tenant
COMM_VOID = "void"         # reservado p/ estorno/cancelamento (Fase 2 — ainda não setado)


class CommissionEntry(Base):
    __tablename__ = "commission_entries"

    id = Column(String, primary_key=True)
    tenant_id = Column(String, nullable=False, index=True)
    walk_id = Column(String, nullable=False, unique=True)  # idempotência: 1 entrada por passeio
    period = Column(String, nullable=False, index=True)     # "YYYY-MM" da finalização

    walk_price = Column(Float, nullable=False)              # base medida (catálogo)
    commission_percent = Column(Float, nullable=False)      # taxa RESOLVIDA (snapshot)
    amount = Column(Float, nullable=False)                  # walk_price * commission_percent/100
    is_network = Column(Boolean, nullable=False, default=False)

    status = Column(String, nullable=False, default=COMM_ACCRUED, index=True)
    asaas_payment_id = Column(String, nullable=True)        # cobrança que faturou esta entrada
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    billed_at = Column(DateTime(timezone=True), nullable=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)


Index("ix_commission_entries_tenant_period_status",
      CommissionEntry.tenant_id, CommissionEntry.period, CommissionEntry.status)
