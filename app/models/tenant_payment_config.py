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

# Fallback legado de comissão quando o plano do tenant é desconhecido.
DEFAULT_COMMISSION_PERCENT = 20.0

# Comissão padrão da plataforma por TIER de plano (white label).
# Override por tenant (commission_is_custom=True) prevalece sobre estes defaults.
PLAN_COMMISSION_DEFAULTS = {"starter": 10.0, "business": 8.0, "enterprise": 5.0}
PLAN_COMMISSION_FALLBACK = 10.0


def commission_default_for_plan(plan: str | None) -> float:
    return PLAN_COMMISSION_DEFAULTS.get((plan or "").strip().lower(), PLAN_COMMISSION_FALLBACK)


class TenantPaymentConfig(Base):
    __tablename__ = "tenant_payment_configs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=False, unique=True, index=True
    )
    provider: Mapped[str] = mapped_column(String, default="asaas")
    # % do valor do passeio que a PLATAFORMA retém (comissão operadora — só super_admin altera).
    commission_percent: Mapped[float] = mapped_column(Float, default=DEFAULT_COMMISSION_PERCENT)
    # % adicional que o TENANT retém sobre o restante (margem do operador white-label).
    # Default 0: resultado idêntico ao comportamento anterior.
    tenant_margin_percent: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    # Quando True, a comissão foi negociada/editada à mão (ex.: Fundador/sócio 0%) e
    # NÃO é sobrescrita pelo default do plano (backfill ou mudança de plano).
    commission_is_custom: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    # Quando True, o split é executado no gateway (walker recebe direto — Fase B).
    split_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
