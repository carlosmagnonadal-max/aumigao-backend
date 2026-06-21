from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user, require_admin
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import ensure_tenant_access, get_admin_tenant_scope
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
    WalkerNetworkInviteResponse,
    WalkerNetworkMeResponse,
    WalkerNetworkProfileResponse,
)
from app.services.tenant_plan_service import enforce_network_access_allowed, tenant_has_feature

router = APIRouter(prefix="/admin/walker-network", tags=["admin-walker-network"], dependencies=[Depends(require_permission("walkers.read"))])
api_router = APIRouter(prefix="/api/admin/walker-network", tags=["admin-walker-network"], dependencies=[Depends(require_permission("walkers.read"))])
# Walker-facing: o passeador deriva do token (get_current_user); ownership por walker_user_id.
walker_router = APIRouter(prefix="/walker/network", tags=["walker-network"])


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
def list_walker_network(
    admin: User = Depends(require_permission("walkers.read")),
    db: Session = Depends(get_db),
):
    # WalkerNetworkProfile nao tem tenant_id — walkers sao globais da plataforma.
    return db.query(WalkerNetworkProfile).order_by(WalkerNetworkProfile.created_at.desc()).all()


@router.get("/tenants/{tenant_id}", response_model=list[TenantWalkerAccessResponse])
@api_router.get("/tenants/{tenant_id}", response_model=list[TenantWalkerAccessResponse])
def list_tenant_walkers(
    tenant_id: str,
    admin: User = Depends(require_permission("walkers.read")),
    db: Session = Depends(get_db),
):
    _tenant_or_404(tenant_id, db)
    # Admin de tenant so pode ver walkers do seu proprio tenant.
    ensure_tenant_access(tenant_id, get_admin_tenant_scope(admin, db))
    return (
        db.query(TenantWalkerAccess)
        .filter(TenantWalkerAccess.tenant_id == tenant_id)
        .order_by(TenantWalkerAccess.created_at.desc())
        .all()
    )


@router.post("/tenants/{tenant_id}", response_model=TenantWalkerAccessResponse)
@api_router.post("/tenants/{tenant_id}", response_model=TenantWalkerAccessResponse)
def link_walker_to_tenant(
    tenant_id: str,
    payload: TenantWalkerAccessCreate,
    admin: User = Depends(require_permission("walkers.manage")),
    db: Session = Depends(get_db),
):
    tenant = _tenant_or_404(tenant_id, db)
    # Escrita: admin de tenant so pode vincular walkers ao seu proprio tenant.
    ensure_tenant_access(tenant_id, get_admin_tenant_scope(admin, db))
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
    admin: User = Depends(require_permission("walkers.manage")),
    db: Session = Depends(get_db),
):
    tenant = _tenant_or_404(tenant_id, db)
    # Escrita: admin de tenant so pode gerir vinculos do seu proprio tenant.
    ensure_tenant_access(tenant_id, get_admin_tenant_scope(admin, db))
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


# ---------------------------------------------------------------------------
# Walker-facing (net-T2/net-T4): o passeador vê e responde os próprios convites.
# ---------------------------------------------------------------------------


def _require_walker(user: User) -> User:
    if user.role not in {"walker", "passeador"}:
        raise HTTPException(status_code=403, detail="Apenas passeadores acessam a Rede.")
    return user


def _own_invite_or_404(invite_id: str, walker_user_id: str, db: Session) -> TenantWalkerAccess:
    invite = (
        db.query(TenantWalkerAccess)
        .filter(
            TenantWalkerAccess.id == invite_id,
            TenantWalkerAccess.walker_user_id == walker_user_id,
        )
        .first()
    )
    if not invite:
        raise HTTPException(status_code=404, detail="Convite nao encontrado.")
    return invite


def _invite_to_response(invite: TenantWalkerAccess, db: Session) -> WalkerNetworkInviteResponse:
    tenant = db.get(Tenant, invite.tenant_id)
    return WalkerNetworkInviteResponse(
        id=invite.id,
        tenant_id=invite.tenant_id,
        tenant_name=tenant.name if tenant else None,
        status=invite.status,
        access_type=invite.access_type,
        invited_at=invite.invited_at,
        responded_at=invite.responded_at,
    )


@walker_router.get("/invites", response_model=list[WalkerNetworkInviteResponse])
def list_my_invites(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_walker(user)
    invites = (
        db.query(TenantWalkerAccess)
        .filter(
            TenantWalkerAccess.walker_user_id == user.id,
            TenantWalkerAccess.status == "pending",
        )
        .order_by(TenantWalkerAccess.invited_at.desc(), TenantWalkerAccess.created_at.desc())
        .all()
    )
    return [_invite_to_response(inv, db) for inv in invites]


def _respond_to_invite(invite_id: str, new_status: str, user: User, db: Session) -> WalkerNetworkInviteResponse:
    _require_walker(user)
    invite = _own_invite_or_404(invite_id, user.id, db)
    if invite.status != "pending":
        raise HTTPException(status_code=409, detail="Convite ja respondido.")
    invite.status = new_status
    invite.responded_at = datetime.utcnow()
    invite.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(invite)
    return _invite_to_response(invite, db)


@walker_router.post("/invites/{invite_id}/accept", response_model=WalkerNetworkInviteResponse)
def accept_invite(invite_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _respond_to_invite(invite_id, "active", user, db)


@walker_router.post("/invites/{invite_id}/decline", response_model=WalkerNetworkInviteResponse)
def decline_invite(invite_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _respond_to_invite(invite_id, "declined", user, db)


@walker_router.get("/me", response_model=WalkerNetworkMeResponse)
def network_me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Plano/capabilities do tenant do passeador (net-T4).

    O app/admin usam network_access para saber se a Rede está disponível.
    """
    _require_walker(user)
    tenant = db.get(Tenant, user.tenant_id) if user.tenant_id else None
    network_access = bool(tenant and tenant_has_feature(tenant, db, "network_access"))
    active_count = (
        db.query(TenantWalkerAccess)
        .filter(
            TenantWalkerAccess.walker_user_id == user.id,
            TenantWalkerAccess.status == "active",
        )
        .count()
    )
    return WalkerNetworkMeResponse(
        tenant_id=user.tenant_id,
        plan=tenant.plan if tenant else None,
        network_access=network_access,
        active_network_tenants=active_count,
    )
