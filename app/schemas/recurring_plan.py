from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class RecurringPlanResponse(ORMModel):
    id: str
    tenant_id: str
    name: str
    description: str | None = None
    price: float
    walks_per_cycle: int
    interval: str
    active: bool
    # Vitrine do app (mig 0102): curadoria do tenant via admin-web.
    featured: bool = False
    display_order: int = 0
    created_at: datetime
    updated_at: datetime


_IntervalLiteral = Literal["weekly", "biweekly", "monthly", "quarterly", "semiannual", "yearly"]


class RecurringPlanCreate(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None
    price: float = Field(ge=0)
    walks_per_cycle: int = Field(ge=0)
    interval: _IntervalLiteral = "monthly"
    active: bool = True
    featured: bool = False
    display_order: int = Field(default=0, ge=0)


class RecurringPlanUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    description: str | None = None
    price: float | None = Field(default=None, ge=0)
    walks_per_cycle: int | None = Field(default=None, ge=0)
    interval: _IntervalLiteral | None = None
    active: bool | None = None
    featured: bool | None = None
    display_order: int | None = Field(default=None, ge=0)


class TutorSubscriptionResponse(ORMModel):
    id: str
    tenant_id: str
    plan_id: str
    tutor_id: str
    status: str
    price: float
    walks_per_cycle: int
    credits_remaining: int
    current_period_start: datetime
    current_period_end: datetime | None = None
    cancelled_at: datetime | None = None
    # ID da subscription nativa no Asaas (Fase 7 $-2) — exposto para debug/admin.
    asaas_subscription_id: str | None = None
    created_at: datetime
    # Conveniência para o app não precisar cruzar com o catálogo.
    plan_name: str | None = None
    # Estado de pagamento derivado: "ativa" quando créditos já foram concedidos
    # (primeiro pagamento confirmado via webhook), "aguardando_pagamento" caso contrário.
    # Nunca persiste — calculado em _subscription_response.
    payment_status: str = "aguardando_pagamento"


class RecurringPlansView(BaseModel):
    """Resposta cliente-final: disponibilidade (feature flag) + catálogo + assinatura atual."""

    available: bool
    plans: list[RecurringPlanResponse]
    subscription: TutorSubscriptionResponse | None = None
