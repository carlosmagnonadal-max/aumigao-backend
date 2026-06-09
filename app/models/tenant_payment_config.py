"""Configuração financeira por tenant (Sprint 16 — White Label gateway-agnóstico).

Cada tenant define seu gateway de pagamento e a comissão que retém de cada
transação. O Aumigão usa Asaas; outros tenants podem usar outros gateways.
Credenciais NÃO ficam aqui em claro — serão referenciadas por secret/env (Fase B).
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Comissão padrão da plataforma quando o tenant não tem config própria.
DEFAULT_COMMISSION_PERCENT = 20.0


class TenantPaymentConfig(Base):
    __tablename__ = "tenant_payment_configs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=False, unique=True, index=True
    )
    provider: Mapped[str] = mapped_column(String, default="asaas")
    # % do valor do passeio que a plataforma/tenant retém (resto vai ao walker).
    commission_percent: Mapped[float] = mapped_column(Float, default=DEFAULT_COMMISSION_PERCENT)
    # Quando True, o split é executado no gateway (walker recebe direto — Fase B).
    split_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
