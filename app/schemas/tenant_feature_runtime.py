from pydantic import BaseModel


class TenantFeatureRuntimeResponse(BaseModel):
    tenant_id: str
    features: dict[str, bool]
