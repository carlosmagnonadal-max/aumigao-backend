from pydantic import BaseModel, Field

from app.models.tenant_payment_config import (
    PLAN_COMMISSION_DEFAULTS,
    PLAN_COMMISSION_FALLBACK,
)


class TenantPaymentConfigResponse(BaseModel):
    tenant_id: str
    provider: str
    commission_percent: float
    commission_is_custom: bool = False
    tenant_margin_percent: float = 0.0
    split_enabled: bool
    active: bool
    # R9: régua de comissão por plano servida pelo backend (fonte canônica), para o
    # admin renderizar sem números fixos. Muda aqui e o front reflete sem editar o front.
    plan_commission_defaults: dict[str, float] = Field(
        default_factory=lambda: dict(PLAN_COMMISSION_DEFAULTS)
    )
    plan_commission_fallback: float = PLAN_COMMISSION_FALLBACK


class TenantPaymentConfigUpdate(BaseModel):
    commission_percent: float | None = None
    tenant_margin_percent: float | None = None
    provider: str | None = None
    split_enabled: bool | None = None
