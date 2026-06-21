"""Rotas de cupons (Onda 2 — monetização).

- Cliente: valida um código (preview do desconto no checkout).
- Admin do tenant: CRUD do catálogo de cupons (finance.*), gated por flag `coupons`.
O resgate (redeem) acontece no checkout/pagamento — ver coupon_service.redeem.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import get_admin_tenant_scope
from app.models.coupon import Coupon
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.coupon import (
    CouponCreate,
    CouponResponse,
    CouponUpdate,
    CouponValidateRequest,
    CouponValidateResult,
)
from app.schemas.metrics import CouponMetricsResponse
from app.services import coupon_service as svc
from app.services.audit_service import record_audit_log
from app.services.metrics_service import get_coupon_metrics
from app.services.tenant_context import resolve_current_tenant, resolve_current_tenant_id

router = APIRouter(prefix="/coupons", tags=["coupons"])
api_router = APIRouter(prefix="/api/coupons", tags=["coupons"])

admin_router = APIRouter(
    prefix="/admin/coupons",
    tags=["coupons-admin"],
    dependencies=[Depends(require_permission("admin.access"))],
)
api_admin_router = APIRouter(
    prefix="/api/admin/coupons",
    tags=["coupons-admin"],
    dependencies=[Depends(require_permission("admin.access"))],
)


def _resolve_user_tenant(user: User, db: Session, request: Request) -> Tenant:
    tenant = resolve_current_tenant(db, request)
    if user.tenant_id and user.tenant_id != tenant.id:
        owned = db.get(Tenant, user.tenant_id)
        if owned:
            return owned
    return tenant


# --------------------------------------------------------------------------- #
# Cliente — validar cupom (preview do desconto)
# --------------------------------------------------------------------------- #
@router.post("/validate", response_model=CouponValidateResult)
@api_router.post("/validate", response_model=CouponValidateResult)
def validate_coupon(payload: CouponValidateRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant = _resolve_user_tenant(user, db, request)
    if not svc.coupons_enabled(tenant, db):
        return CouponValidateResult(valid=False, code=payload.code, final_amount=payload.amount, message="Cupons indisponíveis.")
    return CouponValidateResult(**svc.validate(db, tenant, payload.code, user.id, payload.amount))


# --------------------------------------------------------------------------- #
# Admin do tenant — catálogo
# --------------------------------------------------------------------------- #
def _admin_tenant_id(admin: User, db: Session) -> str:
    scope = get_admin_tenant_scope(admin, db)
    return scope.tenant_id or resolve_current_tenant_id(db)


@admin_router.get("", response_model=list[CouponResponse])
@api_admin_router.get("", response_model=list[CouponResponse])
def admin_list(admin: User = Depends(require_permission("finance.read")), db: Session = Depends(get_db)):
    return svc.list_coupons(db, _admin_tenant_id(admin, db))


@admin_router.post("", response_model=CouponResponse)
@api_admin_router.post("", response_model=CouponResponse)
def admin_create(payload: CouponCreate, admin: User = Depends(require_permission("finance.manage")), db: Session = Depends(get_db)):
    tenant_id = _admin_tenant_id(admin, db)
    data = payload.model_dump()
    data["code"] = (data["code"] or "").strip().upper()
    if svc.get_by_code(db, tenant_id, data["code"]):
        raise HTTPException(status_code=409, detail="Já existe um cupom com esse código.")
    coupon = Coupon(tenant_id=tenant_id, **data)
    db.add(coupon)
    record_audit_log(
        db, action="coupon.created", entity_type="coupon", entity_id=coupon.id,
        actor=admin, after={"code": data["code"]}, tenant_id=tenant_id,
    )
    db.commit()
    db.refresh(coupon)
    return coupon


@admin_router.get("/metrics", response_model=CouponMetricsResponse)
@api_admin_router.get("/metrics", response_model=CouponMetricsResponse)
def admin_coupon_metrics(
    admin: User = Depends(require_permission("finance.read")),
    db: Session = Depends(get_db),
):
    """Métricas de cupons do tenant: totais, top cupons e série semanal de resgates."""
    scope = get_admin_tenant_scope(admin, db)
    data = get_coupon_metrics(db, scope)
    return CouponMetricsResponse(**data)


@admin_router.patch("/{coupon_id}", response_model=CouponResponse)
@api_admin_router.patch("/{coupon_id}", response_model=CouponResponse)
def admin_update(coupon_id: str, payload: CouponUpdate, admin: User = Depends(require_permission("finance.manage")), db: Session = Depends(get_db)):
    tenant_id = _admin_tenant_id(admin, db)
    coupon = svc.get_or_404(db, tenant_id, coupon_id)
    values = payload.model_dump(exclude_unset=True)
    if "code" in values and values["code"]:
        values["code"] = values["code"].strip().upper()
        clash = svc.get_by_code(db, tenant_id, values["code"])
        if clash and clash.id != coupon.id:
            raise HTTPException(status_code=409, detail="Já existe um cupom com esse código.")
    for field, value in values.items():
        setattr(coupon, field, value)
    record_audit_log(
        db, action="coupon.updated", entity_type="coupon", entity_id=coupon.id,
        actor=admin, after=values, tenant_id=tenant_id,
    )
    db.commit()
    db.refresh(coupon)
    return coupon
