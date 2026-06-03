from pydantic import BaseModel, Field


class TenantUnitRuntimeItem(BaseModel):
    id: str
    name: str
    slug: str
    enabled: bool


class TenantUnitRuntimeResponse(BaseModel):
    tenant_id: str
    units: list[TenantUnitRuntimeItem] = Field(default_factory=list)
