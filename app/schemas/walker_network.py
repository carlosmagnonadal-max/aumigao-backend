from datetime import datetime

from pydantic import BaseModel

from app.schemas.common import ORMModel


WALKER_NETWORK_STATUSES = {"active", "suspended", "blocked"}
TENANT_WALKER_ACCESS_TYPES = {"shared_network", "tenant_exclusive"}
TENANT_WALKER_ACCESS_STATUSES = {"active", "paused", "revoked"}


class WalkerNetworkProfileResponse(ORMModel):
    id: str
    walker_user_id: str
    network_status: str
    global_reputation_score: float
    total_completed_walks: int
    total_tenants_served: int
    network_enabled: bool
    created_at: datetime
    updated_at: datetime


class TenantWalkerAccessCreate(BaseModel):
    walker_user_id: str
    access_type: str = "shared_network"
    status: str = "active"


class TenantWalkerAccessUpdate(BaseModel):
    access_type: str | None = None
    status: str | None = None


class TenantWalkerAccessResponse(ORMModel):
    id: str
    tenant_id: str
    walker_user_id: str
    access_type: str
    status: str
    created_at: datetime
    updated_at: datetime
