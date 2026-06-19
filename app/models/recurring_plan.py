"""Planos recorrentes (Onda 1 — mensalidade de passeios).

Domínio cliente-final do White Label: cada tenant define planos recorrentes
(ex.: "8 passeios/mês") e o tutor assina. A assinatura concede créditos de
passeio por ciclo. A cobrança recorrente automática no gateway é integração do
Sprint 16 (Fase B) — aqui modelamos catálogo + ciclo de vida + créditos.

Gated pela feature flag por tenant `recurring_plans` (ver tenant_plan_service).
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _uuid() -> str:
    return str(uuid4())


# Chave de feature flag por tenant que libera os planos recorrentes.
RECURRING_PLANS_FEATURE_KEY = "recurring_plans"

SUBSCRIPTION_ACTIVE = "active"
SUBSCRIPTION_CANCELLED = "cancelled"


class RecurringPlan(Base):
    """Plano recorrente ofertado por um tenant (catálogo)."""

    __tablename__ = "recurring_plans"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Preço mensal do plano (na moeda do tenant).
    price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # Quantidade de passeios incluídos por ciclo (mês/semestre/ano conforme interval).
    walks_per_cycle: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    interval: Mapped[str] = mapped_column(String, nullable=False, default="monthly")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TutorSubscription(Base):
    """Assinatura de um tutor a um plano recorrente."""

    __tablename__ = "tutor_subscriptions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    plan_id: Mapped[str] = mapped_column(String, ForeignKey("recurring_plans.id"), nullable=False, index=True)
    tutor_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default=SUBSCRIPTION_ACTIVE)
    # Snapshots do plano no momento da assinatura (preço/quantidade podem mudar depois).
    price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    walks_per_cycle: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    credits_remaining: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_period_start: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # ID da subscription nativa no Asaas — preenchido quando o pagamento recorrente
    # é criado via API nativa do Asaas (Fase 7 $-2). Nullable: sem Asaas fica None.
    asaas_subscription_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
