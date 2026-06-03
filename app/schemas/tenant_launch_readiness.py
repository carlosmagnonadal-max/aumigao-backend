from pydantic import BaseModel, Field


class TenantLaunchReadinessChecks(BaseModel):
    branding: bool
    app_name: bool
    display_name: bool
    primary_color: bool
    secondary_color: bool
    logo: bool
    icon: bool
    splash: bool
    dedicated_app: bool
    plan: bool
    billing: bool
    units: bool


class TenantLaunchReadinessResponse(BaseModel):
    tenant_id: str
    ready: bool
    score: int
    status: str
    checks: TenantLaunchReadinessChecks
    blocking_items: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    summary: str
