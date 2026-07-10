from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


TENANT_STATUSES = {"draft", "active", "paused", "suspended", "cancelled"}
# Pricing v2: planos canônicos = pro/enterprise. Legados (starter/business)
# mantidos para não invalidar tenants antigos em edição/validação.
# `free` ("Começar"): plano gratuito de captação (R$0, comissão própria 20%, rede
# desligada, sem multiplicadores, cap de passeios) — ver tenant_free_plan_service.
TENANT_PLANS = {"free", "pro", "enterprise", "starter", "business"}
TENANT_UNIT_STATUSES = {"active", "paused", "inactive"}


class TenantCreate(BaseModel):
    name: str
    slug: str
    status: str = "draft"
    plan: str = "pro"
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


VALID_BACKGROUND_CHECK_PROVIDERS = ("manual", "flagcheck", "idwall", "serpro")


class TenantSettingsUpdate(BaseModel):
    timezone: str | None = None
    support_email: str | None = None
    support_phone: str | None = None
    whatsapp_number: str | None = None
    settings_json: str | None = None
    # Provedor de background check plugavel (default "manual").
    # Valores validos: "manual" | "flagcheck" | "idwall" | "serpro".
    # TODO: ao implementar provedor pago, exigir background_check_provider_config
    #       e cifrar as credenciais com Fernet/KMS antes de salvar.
    background_check_provider: str | None = None
    # Motor de cancelamento (mig 0107) — config por tenant (doutrina: tudo
    # configuravel, admin decide). Mesmo padrao do meeting_point_discount (mig
    # 0103): defaults de fabrica ficam no modelo; aqui so a janela de update
    # parcial (None = nao mexe). Bounds validados via Field (janela > 0,
    # percentuais 0-100) — 422 com mensagem do Pydantic, mesmo rigor do
    # background_check_provider acima (validação de escrita antes de salvar).
    cancellation_free_window_minutes: int | None = Field(None, gt=0)
    late_cancellation_fee_percent: float | None = Field(None, ge=0, le=100)
    late_fee_walker_share_percent: float | None = Field(None, ge=0, le=100)
    auto_refund_on_cancel: bool | None = None


class TenantSettingsResponse(ORMModel):
    id: str
    tenant_id: str
    timezone: str
    support_email: str | None = None
    support_phone: str | None = None
    whatsapp_number: str | None = None
    settings_json: str | None = None
    background_check_provider: str = "manual"
    # Motor de cancelamento (mig 0107) — ver TenantSettingsUpdate acima.
    cancellation_free_window_minutes: int = 1440
    late_cancellation_fee_percent: float = 50
    late_fee_walker_share_percent: float = 100
    auto_refund_on_cancel: bool = True
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
