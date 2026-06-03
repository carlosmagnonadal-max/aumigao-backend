from typing import Any

from pydantic import BaseModel


class TenantCommercialPlanResponse(BaseModel):
    key: str
    label: str
    description: str
    capabilities: dict[str, bool]
    recommended_for: list[str]


class TenantCommercialPlansResponse(BaseModel):
    plans: list[TenantCommercialPlanResponse]


class TenantCommercialRuntimeResponse(BaseModel):
    tenant_id: str
    plan: str
    plan_label: str
    capabilities: dict[str, Any]
    features: dict[str, bool]
    upgrade_available: bool
    next_recommended_plan: str | None
    billing_enabled: bool
    billing_status: str
