"""Rotas do Pet Tour (Onda 1 — modalidade especial).

- Cliente-final (tutor): consulta disponibilidade + config (gated pela flag).
- Admin do tenant: configura preço e duração mínima (preço mutável por tenant).

O agendamento em si acontece no POST /walks com modality="pet_tour" (ver walks.py),
que valida via pet_tour_service e aplica o preço do tenant.
"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import get_admin_tenant_scope
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.pet_tour import PetTourConfigResponse, PetTourConfigUpdate, PetTourView
from app.services import pet_tour_service as svc
from app.services.audit_service import record_audit_log
from app.services.tenant_context import resolve_current_tenant, resolve_current_tenant_id

router = APIRouter(prefix="/pet-tour", tags=["pet-tour"])
api_router = APIRouter(prefix="/api/pet-tour", tags=["pet-tour"])

admin_router = APIRouter(
    prefix="/admin/pet-tour-config",
    tags=["pet-tour-admin"],
    dependencies=[Depends(require_permission("admin.access"))],
)
api_admin_router = APIRouter(
    prefix="/api/admin/pet-tour-config",
    tags=["pet-tour-admin"],
    dependencies=[Depends(require_permission("admin.access"))],
)


def _resolve_user_tenant(user: User, db: Session, request: Request) -> Tenant:
    tenant = resolve_current_tenant(db, request)
    if user.tenant_id and user.tenant_id != tenant.id:
        owned = db.get(Tenant, user.tenant_id)
        if owned:
            return owned
    return tenant


def _config_response(config) -> PetTourConfigResponse:
    return PetTourConfigResponse(
        tenant_id=config.tenant_id,
        base_price=config.base_price,
        min_duration_minutes=config.min_duration_minutes,
        active=config.active,
    )


# --------------------------------------------------------------------------- #
# Cliente-final (tutor)
# --------------------------------------------------------------------------- #
@router.get("", response_model=PetTourView)
@api_router.get("", response_model=PetTourView)
def get_pet_tour(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant = _resolve_user_tenant(user, db, request)
    if not svc.pet_tour_enabled(tenant, db):
        return PetTourView(available=False)
    config = svc.get_or_create_config(db, tenant.id)
    db.commit()
    if not config.active:
        return PetTourView(available=False)
    return PetTourView(
        available=True,
        base_price=config.base_price,
        min_duration_minutes=config.min_duration_minutes,
    )


# --------------------------------------------------------------------------- #
# Admin do tenant (configuração)
# --------------------------------------------------------------------------- #
def _admin_tenant_id(admin: User, db: Session) -> str:
    scope = get_admin_tenant_scope(admin)
    return scope.tenant_id or resolve_current_tenant_id(db)


@admin_router.get("", response_model=PetTourConfigResponse)
@api_admin_router.get("", response_model=PetTourConfigResponse)
def get_config(admin: User = Depends(require_permission("finance.read")), db: Session = Depends(get_db)):
    tenant_id = _admin_tenant_id(admin, db)
    config = svc.get_or_create_config(db, tenant_id)
    db.commit()
    return _config_response(config)


@admin_router.put("", response_model=PetTourConfigResponse)
@api_admin_router.put("", response_model=PetTourConfigResponse)
def update_config(
    payload: PetTourConfigUpdate,
    admin: User = Depends(require_permission("finance.manage")),
    db: Session = Depends(get_db),
):
    tenant_id = _admin_tenant_id(admin, db)
    config = svc.get_or_create_config(db, tenant_id)
    values = payload.model_dump(exclude_unset=True)
    for field, value in values.items():
        setattr(config, field, value)
    record_audit_log(
        db, action="pet_tour_config.updated", entity_type="tenant_pet_tour_config", entity_id=tenant_id,
        actor=admin, after=values, tenant_id=tenant_id,
    )
    db.commit()
    db.refresh(config)
    return _config_response(config)
