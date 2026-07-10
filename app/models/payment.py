from datetime import datetime
from sqlalchemy import DateTime, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base
from app.models.types import Money

class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    tutor_id: Mapped[str] = mapped_column(String, index=True)
    walk_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    amount: Mapped[float] = mapped_column(Money)
    status: Mapped[str] = mapped_column(String, default="pending")
    provider: Mapped[str] = mapped_column(String, default="asaas")
    provider_payment_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # URL da fatura/checkout hospedado retornada pelo Asaas (invoiceUrl).
    # Persiste para exibição posterior sem nova chamada ao gateway.
    invoice_url: Mapped[str | None] = mapped_column(String, nullable=True)
    # Split de receita (Sprint 16): como o valor se divide entre plataforma e walker.
    commission_percent: Mapped[float | None] = mapped_column(Numeric(5, 2, asdecimal=False), nullable=True)
    platform_amount: Mapped[float | None] = mapped_column(Money, nullable=True)
    walker_amount: Mapped[float | None] = mapped_column(Money, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Migration 0107 — motor de cancelamento: rastreio do estorno solicitado pelo
    # motor de cancelamento (cancel_walk_service). NULL = nunca houve pedido de
    # estorno neste payment (zero-regressão nos payments existentes).
    # "pending" = solicitado ao Asaas, aguardando confirmação via webhook;
    # "done" = confirmado (webhook PAYMENT_REFUNDED/PAYMENT_PARTIALLY_REFUNDED);
    # "failed" = a chamada ao gateway falhou (best-effort; não desfaz o cancelamento —
    # ver record_operational_log no motor para retry/visibilidade admin).
    refund_status: Mapped[str | None] = mapped_column(String, nullable=True)
    refunded_amount: Mapped[float | None] = mapped_column(Money, nullable=True)
