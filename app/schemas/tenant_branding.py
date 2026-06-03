from pydantic import BaseModel


class TenantBrandingRuntimeResponse(BaseModel):
    tenant_id: str
    display_name: str
    app_name: str
    logo_url: str
    icon_url: str
    primary_color: str
    secondary_color: str
    powered_by_enabled: bool
