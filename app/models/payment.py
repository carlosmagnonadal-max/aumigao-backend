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
