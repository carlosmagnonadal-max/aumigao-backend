from datetime import datetime
from uuid import uuid4
from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base
from app.models.types import Money

def _uuid() -> str:
    return str(uuid4())

# revenue_type values
REVENUE_WALK_COMMISSION = "walk_commission"
REVENUE_SAAS_SUBSCRIPTION = "saas_subscription"
REVENUE_TIP = "tip"

class TenantFiscalConfig(Base):
    __tablename__ = "tenant_fiscal_config"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False, unique=True, index=True)
    # Provisão (percentuais; default 0 -> provisão zero até configurar)
    commission_tax_percent: Mapped[float] = mapped_column(Money, default=0, nullable=False)
    subscription_tax_percent: Mapped[float] = mapped_column(Money, default=0, nullable=False)
    walker_tax_percent: Mapped[float] = mapped_column(Money, default=0, nullable=False)
    # Estruturais NFS-e (usados quando a emissão ligar)
    iss_percent: Mapped[float | None] = mapped_column(Money, nullable=True)
    municipal_service_code: Mapped[str | None] = mapped_column(String, nullable=True)
    simples_nacional: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    cnae: Mapped[str | None] = mapped_column(String, nullable=True)
    service_description: Mapped[str | None] = mapped_column(String, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class PaymentProvision(Base):
    __tablename__ = "payment_provision"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    payment_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    revenue_type: Mapped[str] = mapped_column(String, nullable=False)
    walker_gross: Mapped[float] = mapped_column(Money, default=0, nullable=False)
    walker_tax: Mapped[float] = mapped_column(Money, default=0, nullable=False)
    walker_net: Mapped[float] = mapped_column(Money, default=0, nullable=False)
    platform_gross: Mapped[float] = mapped_column(Money, default=0, nullable=False)
    platform_tax: Mapped[float] = mapped_column(Money, default=0, nullable=False)
    platform_net: Mapped[float] = mapped_column(Money, default=0, nullable=False)
    walker_tax_percent_applied: Mapped[float] = mapped_column(Money, default=0, nullable=False)
    platform_tax_percent_applied: Mapped[float] = mapped_column(Money, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
