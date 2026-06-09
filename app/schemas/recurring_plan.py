from datetime import datetime

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
    created_at: datetime
    updated_at: datetime


class RecurringPlanCreate(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None
    price: float = Field(ge=0)
    walks_per_cycle: int = Field(ge=0)
    interval: str = "monthly"
    active: bool = True


class RecurringPlanUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    description: str | None = None
    price: float | None = Field(default=None, ge=0)
    walks_per_cycle: int | None = Field(default=None, ge=0)
    interval: str | None = None
    active: bool | None = None


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
    created_at: datetime
    # Conveniência para o app não precisar cruzar com o catálogo.
    plan_name: str | None = None


class RecurringPlansView(BaseModel):
    """Resposta cliente-final: disponibilidade (feature flag) + catálogo + assinatura atual."""

    available: bool
    plans: list[RecurringPlanResponse]
    subscription: TutorSubscriptionResponse | None = None
