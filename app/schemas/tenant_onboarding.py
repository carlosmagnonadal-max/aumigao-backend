from datetime import datetime

from pydantic import BaseModel

from app.schemas.common import ORMModel


TENANT_ONBOARDING_STATUSES = {
    "created",
    "contract_pending",
    "contract_signed",
    "setup_pending",
    "branding_pending",
    "unit_pending",
    "operator_pending",
    "ready_for_launch",
    "live",
}


class TenantOnboardingUpdate(BaseModel):
    onboarding_status: str | None = None
    contract_signed: bool | None = None
    setup_paid: bool | None = None
    branding_completed: bool | None = None
    units_completed: bool | None = None
    operators_completed: bool | None = None
    go_live_approved: bool | None = None


class TenantOnboardingResponse(ORMModel):
    id: str
    tenant_id: str
    onboarding_status: str
    contract_signed: bool
    setup_paid: bool
    branding_completed: bool
    units_completed: bool
    operators_completed: bool
    go_live_approved: bool
    created_at: datetime
    updated_at: datetime
