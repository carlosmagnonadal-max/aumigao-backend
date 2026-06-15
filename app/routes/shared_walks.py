"""Rotas dos passeios compartilhados (Onda 1).

- Cliente (tutor): cria sessão (convite), outro tutor entra, paga a cota, confirma.
- Admin do tenant: configura preço por pet, limites e o toggle do pool.
"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import get_admin_tenant_scope
from app.models.pet import Pet
from app.models.shared_walk import SHARED_WALKS_FEATURE_KEY, SharedWalk
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.shared_walk import (
    SharedWalkConfigResponse,
    SharedWalkConfigUpdate,
    SharedWalkCreate,
    SharedWalkJoin,
    SharedWalkResponse,
    SharedWalkView,
)
from app.services import shared_walk_service as svc
from app.services.audit_service import record_audit_log
from app.services.tenant_context import resolve_current_tenant, resolve_current_tenant_id
from app.services.tenant_plan_service import enforce_plan_allows_product_feature

router = APIRouter(prefix="/shared-walks", tags=["shared-walks"])
api_router = APIRouter(prefix="/api/shared-walks", tags=["shared-walks"])

admin_router = APIRouter(
    prefix="/admin/shared-walk-config",
    tags=["shared-walks-admin"],
    dependencies=[Depends(require_permission("admin.access"))],
)
api_admin_router = APIRouter(
    prefix="/api/admin/shared-walk-config",
    tags=["shared-walks-admin"],
    dependencies=[Depends(require_permission("admin.access"))],
)


def _resolve_user_tenant(user: User, db: Session, request: Request) -> Tenant:
    tenant = resolve_current_tenant(db, request)
    if user.tenant_id and user.tenant_id != tenant.id:
        owned = db.get(Tenant, user.tenant_id)
        if owned:
            return owned
    return tenant


def _session_response(db: Session, session: SharedWalk) -> SharedWalkResponse:
    response = SharedWalkResponse.model_validate(session)
    names = {}
    for p in response.participants:
        if p.pet_id not in names:
            pet = db.get(Pet, p.pet_id)
            names[p.pet_id] = pet.name if pet else None
        p.pet_name = names[p.pet_id]
    response.tutor_count = svc.tutor_count(session)
    return response


def _config_response(config) -> SharedWalkConfigResponse:
    return SharedWalkConfigResponse(
        tenant_id=config.tenant_id,
        price_per_pet=config.price_per_pet,
        price_30=config.price_30,
        price_45=config.price_45,
        price_60=config.price_60,
        max_pets_same_tutor=config.max_pets_same_tutor,
        max_tutors=config.max_tutors,
        pool_enabled=config.pool_enabled,
        pool_radius_km=config.pool_radius_km,
        pool_time_window_min=config.pool_time_window_min,
        active=config.active,
    )


# --------------------------------------------------------------------------- #
# Cliente
# --------------------------------------------------------------------------- #
@router.get("", response_model=SharedWalkView)
@api_router.get("", response_model=SharedWalkView)
def list_shared_walks(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant = _resolve_user_tenant(user, db, request)
    if not svc.shared_walks_enabled(tenant, db):
        return SharedWalkView(available=False)
    config = svc.get_or_create_config(db, tenant.id)
    db.commit()
    if not config.active:
        return SharedWalkView(available=False)
    sessions = svc.list_my_sessions(db, tenant.id, user.id)
    return SharedWalkView(
        available=True,
        price_per_pet=config.price_per_pet,
        price_30=config.price_30,
        price_45=config.price_45,
        price_60=config.price_60,
        max_tutors=config.max_tutors,
        max_pets_same_tutor=config.max_pets_same_tutor,
        pool_enabled=config.pool_enabled,
        sessions=[_session_response(db, s) for s in sessions],
    )


@router.post("", response_model=SharedWalkResponse)
@api_router.post("", response_model=SharedWalkResponse)
def create_shared_walk(payload: SharedWalkCreate, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant = _resolve_user_tenant(user, db, request)
    session = svc.create_session(
        db, tenant, user.id,
        scheduled_date=payload.scheduled_date,
        duration_minutes=payload.duration_minutes,
        host_pet_ids=payload.host_pet_ids,
        open_to_pool=payload.open_to_pool,
    )
    return _session_response(db, session)


@router.get("/{walk_id}", response_model=SharedWalkResponse)
@api_router.get("/{walk_id}", response_model=SharedWalkResponse)
def get_shared_walk(walk_id: str, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant = _resolve_user_tenant(user, db, request)
    return _session_response(db, svc.get_session_or_404(db, tenant.id, walk_id))


@router.post("/{walk_id}/join", response_model=SharedWalkResponse)
@api_router.post("/{walk_id}/join", response_model=SharedWalkResponse)
def join_shared_walk(walk_id: str, payload: SharedWalkJoin, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant = _resolve_user_tenant(user, db, request)
    return _session_response(db, svc.join_session(db, tenant, walk_id, user.id, payload.pet_id))


@router.post("/{walk_id}/checkout", response_model=SharedWalkResponse)
@api_router.post("/{walk_id}/checkout", response_model=SharedWalkResponse)
def checkout_shared_walk(walk_id: str, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant = _resolve_user_tenant(user, db, request)
    return _session_response(db, svc.checkout(db, tenant, walk_id, user.id))


@router.post("/{walk_id}/confirm", response_model=SharedWalkResponse)
@api_router.post("/{walk_id}/confirm", response_model=SharedWalkResponse)
def confirm_shared_walk(walk_id: str, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant = _resolve_user_tenant(user, db, request)
    return _session_response(db, svc.confirm_session(db, tenant, walk_id, user.id))


@router.post("/{walk_id}/cancel", response_model=SharedWalkResponse)
@api_router.post("/{walk_id}/cancel", response_model=SharedWalkResponse)
def cancel_shared_walk(walk_id: str, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant = _resolve_user_tenant(user, db, request)
    return _session_response(db, svc.cancel_participation(db, tenant, walk_id, user.id))


# --------------------------------------------------------------------------- #
# Admin do tenant
# --------------------------------------------------------------------------- #
def _admin_tenant_id(admin: User, db: Session) -> str:
    scope = get_admin_tenant_scope(admin)
    return scope.tenant_id or resolve_current_tenant_id(db)


@admin_router.get("", response_model=SharedWalkConfigResponse)
@api_admin_router.get("", response_model=SharedWalkConfigResponse)
def get_config(admin: User = Depends(require_permission("finance.read")), db: Session = Depends(get_db)):
    tenant_id = _admin_tenant_id(admin, db)
    config = svc.get_or_create_config(db, tenant_id)
    db.commit()
    return _config_response(config)


@admin_router.put("", response_model=SharedWalkConfigResponse)
@api_admin_router.put("", response_model=SharedWalkConfigResponse)
def update_config(payload: SharedWalkConfigUpdate, admin: User = Depends(require_permission("finance.manage")), db: Session = Depends(get_db)):
    tenant_id = _admin_tenant_id(admin, db)
    tenant = db.get(Tenant, tenant_id)
    if tenant is not None:
        enforce_plan_allows_product_feature(tenant, SHARED_WALKS_FEATURE_KEY, "Passeios compartilhados")
    config = svc.get_or_create_config(db, tenant_id)
    values = payload.model_dump(exclude_unset=True)
    for field, value in values.items():
        setattr(config, field, value)
    record_audit_log(
        db, action="shared_walk_config.updated", entity_type="tenant_shared_walk_config", entity_id=tenant_id,
        actor=admin, after=values, tenant_id=tenant_id,
    )
    db.commit()
    db.refresh(config)
    return _config_response(config)
