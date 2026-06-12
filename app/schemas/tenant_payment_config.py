from pydantic import BaseModel


class TenantPaymentConfigResponse(BaseModel):
    tenant_id: str
    provider: str
    commission_percent: float
    tenant_margin_percent: float = 0.0
    split_enabled: bool
    active: bool


class TenantPaymentConfigUpdate(BaseModel):
    commission_percent: float | None = None
    tenant_margin_percent: float | None = None
    provider: str | None = None
    split_enabled: bool | None = None
