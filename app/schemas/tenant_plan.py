from typing import Any

from pydantic import BaseModel


class TenantCapabilitiesResponse(BaseModel):
    tenant_id: str
    plan: str
    capabilities: dict[str, Any]
