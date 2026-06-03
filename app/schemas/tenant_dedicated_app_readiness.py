from typing import Any

from pydantic import BaseModel, Field

from app.schemas.tenant_app_config import (
    TenantAppBrandingConfig,
    TenantAppCommercialConfig,
    TenantAppFeaturesConfig,
)


class TenantDedicatedAppAssetReadiness(BaseModel):
    logo_missing: bool
    icon_missing: bool
    splash_missing: bool


class TenantDedicatedAppReadinessResponse(BaseModel):
    tenant_id: str
    ready_for_dedicated_app: bool
    dedicated_app_enabled: bool
    missing: list[str] = Field(default_factory=list)
    asset_readiness: TenantDedicatedAppAssetReadiness
    branding: TenantAppBrandingConfig
    commercial: TenantAppCommercialConfig
    features: TenantAppFeaturesConfig
    capabilities: dict[str, Any] = Field(default_factory=dict)
