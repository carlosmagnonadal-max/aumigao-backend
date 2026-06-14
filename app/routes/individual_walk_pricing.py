"""Rotas do preço de passeio individual por tenant (white label).

- Cliente (tutor): lê os preços das durações 30/45/60 do seu tenant.
- Admin do tenant: vê/edita os preços. Espelha shared_walks.py.
"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import get_admin_tenant_scope
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.individual_walk_pricing import (
    IndividualWalkPricingResponse,
    IndividualWalkPricingUpdate,
)
from app.services import individual_walk_pricing_service as svc
from app.services.audit_service import record_audit_log
from app.services.tenant_context import resolve_current_tenant, resolve_current_tenant_id

router = APIRouter(prefix="/individual-walk-pricing", tags=["individual-walk-pricing"])
api_router = APIRouter(prefix="/api/individual-walk-pricing", tags=["individual-walk-pricing"])

admin_router = APIRouter(
    prefix="/admin/individual-walk-pricing",
    tags=["individual-walk-pricing-admin"],
    dependencies=[Depends(require_permission("admin.access"))],
)
api_admin_router = APIRouter(
    prefix="/api/admin/individual-walk-pricing",
    tags=["individual-walk-pricing-admin"],
    dependencies=[Depends(require_permission("admin.access"))],
)


def _resolve_user_tenant(user: User, db: Session, request: Request) -> Tenant:
    tenant = resolve_current_tenant(db, request)
    if user.tenant_id and user.tenant_id != tenant.id:
        owned = db.get(Tenant, user.tenant_id)
        if owned:
            return owned
    return tenant


def _config_response(config) -> IndividualWalkPricingResponse:
    return IndividualWalkPricingResponse(
        tenant_id=config.tenant_id,
        price_30=config.price_30,
        price_45=config.price_45,
        price_60=config.price_60,
        active=config.active,
    )


# --------------------------------------------------------------------------- #
# Cliente
# --------------------------------------------------------------------------- #
@router.get("", response_model=IndividualWalkPricingResponse)
@api_router.get("", response_model=IndividualWalkPricingResponse)
def get_individual_pricing(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant = _resolve_user_tenant(user, db, request)
    config = svc.get_or_create_config(db, tenant.id)
    db.commit()
    return _config_response(config)


# --------------------------------------------------------------------------- #
# Admin do tenant
# --------------------------------------------------------------------------- #
def _admin_tenant_id(admin: User, db: Session) -> str:
    scope = get_admin_tenant_scope(admin)
    return scope.tenant_id or resolve_current_tenant_id(db)


@admin_router.get("", response_model=IndividualWalkPricingResponse)
@api_admin_router.get("", response_model=IndividualWalkPricingResponse)
def get_config(admin: User = Depends(require_permission("finance.read")), db: Session = Depends(get_db)):
    tenant_id = _admin_tenant_id(admin, db)
    config = svc.get_or_create_config(db, tenant_id)
    db.commit()
    return _config_response(config)


@admin_router.put("", response_model=IndividualWalkPricingResponse)
@api_admin_router.put("", response_model=IndividualWalkPricingResponse)
def update_config(payload: IndividualWalkPricingUpdate, admin: User = Depends(require_permission("finance.manage")), db: Session = Depends(get_db)):
    tenant_id = _admin_tenant_id(admin, db)
    config = svc.get_or_create_config(db, tenant_id)
    values = payload.model_dump(exclude_unset=True)
    for field, value in values.items():
        setattr(config, field, value)
    record_audit_log(
        db, action="individual_walk_pricing.updated", entity_type="tenant_individual_walk_pricing", entity_id=tenant_id,
        actor=admin, after=values, tenant_id=tenant_id,
    )
    db.commit()
    db.refresh(config)
    return _config_response(config)
