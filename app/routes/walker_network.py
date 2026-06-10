from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import require_admin
from app.dependencies.rbac import require_permission
from app.models.tenant import Tenant
from app.models.tenant_walker_access import TenantWalkerAccess
from app.models.user import User
from app.models.walker_network_profile import WalkerNetworkProfile
from app.schemas.walker_network import (
    TENANT_WALKER_ACCESS_STATUSES,
    TENANT_WALKER_ACCESS_TYPES,
    TenantWalkerAccessCreate,
    TenantWalkerAccessResponse,
    TenantWalkerAccessUpdate,
    WalkerNetworkProfileResponse,
)
from app.services.tenant_plan_service import enforce_network_access_allowed

router = APIRouter(prefix="/admin/walker-network", tags=["admin-walker-network"], dependencies=[Depends(require_permission("walkers.read"))])
api_router = APIRouter(prefix="/api/admin/walker-network", tags=["admin-walker-network"], dependencies=[Depends(require_permission("walkers.read"))])


def _ensure_choice(value: str | None, allowed: set[str], field_name: str) -> None:
    if value is not None and value not in allowed:
        raise HTTPException(status_code=400, detail=f"{field_name} invalido.")


def _tenant_or_404(tenant_id: str, db: Session) -> Tenant:
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant nao encontrado.")
    return tenant


def _walker_or_404(walker_user_id: str, db: Session) -> User:
    walker = db.get(User, walker_user_id)
    if not walker or walker.role != "walker":
        raise HTTPException(status_code=404, detail="Passeador nao encontrado.")
    return walker


def _ensure_network_profile(walker_user_id: str, db: Session) -> WalkerNetworkProfile:
    profile = db.query(WalkerNetworkProfile).filter(WalkerNetworkProfile.walker_user_id == walker_user_id).first()
    if profile:
        return profile
    profile = WalkerNetworkProfile(walker_user_id=walker_user_id)
    db.add(profile)
    return profile


@router.get("", response_model=list[WalkerNetworkProfileResponse])
@api_router.get("", response_model=list[WalkerNetworkProfileResponse])
def list_walker_network(db: Session = Depends(get_db)):
    return db.query(WalkerNetworkProfile).order_by(WalkerNetworkProfile.created_at.desc()).all()


@router.get("/tenants/{tenant_id}", response_model=list[TenantWalkerAccessResponse])
@api_router.get("/tenants/{tenant_id}", response_model=list[TenantWalkerAccessResponse])
def list_tenant_walkers(tenant_id: str, db: Session = Depends(get_db)):
    _tenant_or_404(tenant_id, db)
    return (
        db.query(TenantWalkerAccess)
        .filter(TenantWalkerAccess.tenant_id == tenant_id)
        .order_by(TenantWalkerAccess.created_at.desc())
        .all()
    )


@router.post("/tenants/{tenant_id}", response_model=TenantWalkerAccessResponse)
@api_router.post("/tenants/{tenant_id}", response_model=TenantWalkerAccessResponse)
def link_walker_to_tenant(tenant_id: str, payload: TenantWalkerAccessCreate, db: Session = Depends(get_db)):
    tenant = _tenant_or_404(tenant_id, db)
    enforce_network_access_allowed(tenant, db)
    _walker_or_404(payload.walker_user_id, db)
    _ensure_choice(payload.access_type, TENANT_WALKER_ACCESS_TYPES, "access_type")
    _ensure_choice(payload.status, TENANT_WALKER_ACCESS_STATUSES, "status")
    _ensure_network_profile(payload.walker_user_id, db)

    access = (
        db.query(TenantWalkerAccess)
        .filter(TenantWalkerAccess.tenant_id == tenant_id, TenantWalkerAccess.walker_user_id == payload.walker_user_id)
        .first()
    )
    if not access:
        access = TenantWalkerAccess(tenant_id=tenant_id, walker_user_id=payload.walker_user_id)
        db.add(access)

    access.access_type = payload.access_type
    access.status = payload.status
    access.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(access)
    return access


@router.patch("/tenants/{tenant_id}/walkers/{walker_user_id}", response_model=TenantWalkerAccessResponse)
@api_router.patch("/tenants/{tenant_id}/walkers/{walker_user_id}", response_model=TenantWalkerAccessResponse)
def update_tenant_walker_access(
    tenant_id: str,
    walker_user_id: str,
    payload: TenantWalkerAccessUpdate,
    db: Session = Depends(get_db),
):
    tenant = _tenant_or_404(tenant_id, db)
    # Consistente com o POST: gerir vinculos da Rede exige que o plano libere network_access.
    enforce_network_access_allowed(tenant, db)
    _walker_or_404(walker_user_id, db)
    access = (
        db.query(TenantWalkerAccess)
        .filter(TenantWalkerAccess.tenant_id == tenant_id, TenantWalkerAccess.walker_user_id == walker_user_id)
        .first()
    )
    if not access:
        raise HTTPException(status_code=404, detail="Acesso do passeador ao tenant nao encontrado.")

    values = payload.model_dump(exclude_unset=True)
    _ensure_choice(values.get("access_type"), TENANT_WALKER_ACCESS_TYPES, "access_type")
    _ensure_choice(values.get("status"), TENANT_WALKER_ACCESS_STATUSES, "status")
    for field, value in values.items():
        setattr(access, field, value)
    access.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(access)
    return access
