"""Assinatura de mensalidade SaaS do tenant (Projeto B)."""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _uuid() -> str:
    return str(uuid4())

SAAS_ACTIVE = "active"
SAAS_OVERDUE = "overdue"
SAAS_CANCELLED = "cancelled"


class TenantSaasSubscription(Base):
    __tablename__ = "tenant_saas_subscriptions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    plan: Mapped[str] = mapped_column(String, nullable=False)
    price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    status: Mapped[str] = mapped_column(String, nullable=False, default=SAAS_ACTIVE)
    asaas_subscription_id: Mapped[str | None] = mapped_column(String, nullable=True)
    current_period_start: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_payment_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    overdue_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
