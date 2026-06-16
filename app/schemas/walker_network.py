from datetime import datetime

from pydantic import BaseModel

from app.schemas.common import ORMModel


WALKER_NETWORK_STATUSES = {"active", "suspended", "blocked"}
TENANT_WALKER_ACCESS_TYPES = {"shared_network", "tenant_exclusive"}
# Estados do convite à Rede: pending/active/declined/revoked.
# "paused" e "active" preservados para compatibilidade com dados legados.
TENANT_WALKER_ACCESS_STATUSES = {"pending", "active", "declined", "revoked", "paused"}
# Apenas estes contam como "na rede do tenant" para fins de matching.
TENANT_WALKER_ACCESS_ACTIVE_STATUSES = {"active"}


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
    invited_at: datetime | None = None
    responded_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class WalkerNetworkInviteResponse(ORMModel):
    """Convite à Rede visto pelo passeador (walker-facing)."""

    id: str
    tenant_id: str
    tenant_name: str | None = None
    status: str
    access_type: str
    invited_at: datetime | None = None
    responded_at: datetime | None = None


class WalkerNetworkMeResponse(BaseModel):
    """Plano/capabilities do tenant do passeador autenticado (net-T4)."""

    tenant_id: str
    plan: str | None = None
    network_access: bool
    active_network_tenants: int
