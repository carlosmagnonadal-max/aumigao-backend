from datetime import datetime

from pydantic import BaseModel

from app.enums import TenantWalkerAccessStatus
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
    # api-T1: mantido como str crua de proposito. A rota valida o status
    # manualmente e devolve 400 em valor invalido — tipar como StrEnum aqui
    # mudaria esse contrato para 422 (Pydantic). Os valores canonicos vivem em
    # app.enums.TenantWalkerAccessStatus (espelhado por TENANT_WALKER_ACCESS_STATUSES).
    status: str = TenantWalkerAccessStatus.ACTIVE.value


class TenantWalkerAccessUpdate(BaseModel):
    access_type: str | None = None
    status: str | None = None


class TenantWalkerAccessResponse(ORMModel):
    id: str
    tenant_id: str
    walker_user_id: str
    access_type: str
    status: str
    # F3.2: gate de requisitos extras por tenant (fila de aprovação no admin-web).
    requirements_met: bool = True
    requirements_submitted_at: datetime | None = None
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
    # C8: cor da marca do tenant (para chip colorido no app) e mensagem opcional.
    tenant_brand_color: str | None = None
    message: str | None = None


class WalkerNetworkMeResponse(BaseModel):
    """Plano/capabilities do tenant do passeador autenticado (net-T4)."""

    tenant_id: str
    plan: str | None = None
    network_access: bool
    active_network_tenants: int
