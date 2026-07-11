from typing import Any

from pydantic import BaseModel, Field

from app.schemas.tenant_unit_runtime import TenantUnitRuntimeItem


class TenantAppBrandingConfig(BaseModel):
    display_name: str
    app_name: str
    logo_url: str
    icon_url: str
    splash_image_url: str
    primary_color: str
    secondary_color: str
    accent_color: str = ""
    powered_by_enabled: bool


class TenantAppFeaturesConfig(BaseModel):
    network_access: bool
    dedicated_app: bool
    custom_products: bool
    custom_projects: bool


class TenantAppCommercialConfig(BaseModel):
    plan: str
    plan_label: str
    upgrade_available: bool
    next_recommended_plan: str | None
    billing_enabled: bool
    billing_status: str


class TenantAppConfigResponse(BaseModel):
    tenant_id: str
    branding: TenantAppBrandingConfig
    features: TenantAppFeaturesConfig
    units: list[TenantUnitRuntimeItem] = Field(default_factory=list)
    commercial: TenantAppCommercialConfig
    capabilities: dict[str, Any] = Field(default_factory=dict)
