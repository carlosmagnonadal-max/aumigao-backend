from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


TENANT_STATUSES = {"draft", "active", "paused", "suspended", "cancelled"}
TENANT_PLANS = {"starter", "business", "enterprise"}
TENANT_UNIT_STATUSES = {"active", "paused", "inactive"}


class TenantCreate(BaseModel):
    name: str
    slug: str
    status: str = "draft"
    plan: str = "starter"
    legal_name: str | None = None
    document_number: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None


class TenantUpdate(BaseModel):
    name: str | None = None
    status: str | None = None
    plan: str | None = None
    legal_name: str | None = None
    document_number: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None


class TenantResponse(ORMModel):
    id: str
    name: str
    slug: str
    status: str
    plan: str
    legal_name: str | None = None
    document_number: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    created_at: datetime
    updated_at: datetime


class TenantBrandingUpdate(BaseModel):
    display_name: str | None = None
    app_name: str | None = None
    logo_url: str | None = None
    icon_url: str | None = None
    splash_image_url: str | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    accent_color: str | None = None
    powered_by_enabled: bool | None = None


class TenantBrandingResponse(ORMModel):
    id: str
    tenant_id: str
    display_name: str
    app_name: str | None = None
    logo_url: str | None = None
    icon_url: str | None = None
    splash_image_url: str | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    accent_color: str | None = None
    powered_by_enabled: bool
    created_at: datetime
    updated_at: datetime


class TenantFeatureUpdate(BaseModel):
    feature_key: str
    enabled: bool = False
    limit_value: str | None = None
    metadata_json: str | None = None


class TenantFeatureResponse(ORMModel):
    id: str
    tenant_id: str
    feature_key: str
    enabled: bool
    limit_value: str | None = None
    metadata_json: str | None = None
    created_at: datetime
    updated_at: datetime


class TenantSettingsUpdate(BaseModel):
    timezone: str | None = None
    support_email: str | None = None
    support_phone: str | None = None
    whatsapp_number: str | None = None
    settings_json: str | None = None


class TenantSettingsResponse(ORMModel):
    id: str
    tenant_id: str
    timezone: str
    support_email: str | None = None
    support_phone: str | None = None
    whatsapp_number: str | None = None
    settings_json: str | None = None
    created_at: datetime
    updated_at: datetime


class TenantUnitCreate(BaseModel):
    name: str
    status: str = "active"
    city: str | None = None
    state: str | None = None


class TenantUnitResponse(ORMModel):
    id: str
    tenant_id: str
    name: str
    status: str
    city: str | None = None
    state: str | None = None
    created_at: datetime
    updated_at: datetime


class TenantDetailResponse(TenantResponse):
    branding: TenantBrandingResponse | None = None
    settings: TenantSettingsResponse | None = None
    features: list[TenantFeatureResponse] = Field(default_factory=list)
    units: list[TenantUnitResponse] = Field(default_factory=list)
