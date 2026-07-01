from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import get_admin_tenant_scope
from app.models.tutor_referral import TutorReferralConfig
from app.models.user import User
from app.services import tutor_referral_config_service as svc

admin_router = APIRouter(
    prefix="/admin/tutor-referral-config",
    tags=["tutor-referral-admin"],
    dependencies=[Depends(require_permission("admin.access"))],
)
api_admin_router = APIRouter(
    prefix="/api/admin/tutor-referral-config",
    tags=["tutor-referral-admin"],
    dependencies=[Depends(require_permission("admin.access"))],
)


class TutorReferralConfigResponse(BaseModel):
    tenant_id: str
    enabled: bool
    reward_type: str
    discount_kind: str
    discount_value: float
    free_walks_count: int
    credit_walks: int
    same_reward_both_sides: bool
    referrer_multiplier: float
    referred_multiplier: float
    trigger_type: str
    trigger_n: int


class TutorReferralConfigUpdate(BaseModel):
    enabled: bool | None = None
    reward_type: str | None = None
    discount_kind: str | None = None
    discount_value: float | None = None
    free_walks_count: int | None = None
    credit_walks: int | None = None
    same_reward_both_sides: bool | None = None
    referrer_multiplier: float | None = None
    referred_multiplier: float | None = None
    trigger_type: str | None = None
    trigger_n: int | None = None


def _response(cfg: TutorReferralConfig) -> TutorReferralConfigResponse:
    return TutorReferralConfigResponse(
        tenant_id=cfg.tenant_id,
        enabled=cfg.enabled,
        reward_type=cfg.reward_type,
        discount_kind=cfg.discount_kind,
        discount_value=cfg.discount_value,
        free_walks_count=cfg.free_walks_count,
        credit_walks=cfg.credit_walks,
        same_reward_both_sides=cfg.same_reward_both_sides,
        referrer_multiplier=cfg.referrer_multiplier,
        referred_multiplier=cfg.referred_multiplier,
        trigger_type=cfg.trigger_type,
        trigger_n=cfg.trigger_n,
    )


def _tenant_id(admin: User, db: Session) -> str:
    scope = get_admin_tenant_scope(admin, db)
    # Super-admin global (sem act-as) cai aqui com scope.tenant_id=None;
    # usamos o tenant_id do próprio user como fallback (análogo a pet_tour).
    tenant_id = scope.tenant_id or getattr(admin, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id obrigatório para admin global.")
    return tenant_id


@admin_router.get("", response_model=TutorReferralConfigResponse)
@api_admin_router.get("", response_model=TutorReferralConfigResponse)
def get_config(
    admin: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(admin, db)
    cfg = svc.get_or_create_tutor_referral_config(db, tenant_id)
    db.commit()
    return _response(cfg)


@admin_router.put("", response_model=TutorReferralConfigResponse)
@api_admin_router.put("", response_model=TutorReferralConfigResponse)
def update_config(
    payload: TutorReferralConfigUpdate,
    admin: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(admin, db)
    values = payload.model_dump(exclude_unset=True)
    svc.validate_config_update(values)
    cfg = svc.get_or_create_tutor_referral_config(db, tenant_id)
    for field, value in values.items():
        setattr(cfg, field, value)
    db.commit()
    db.refresh(cfg)
    return _response(cfg)


metrics_admin_router = APIRouter(
    prefix="/admin/tutor-referral",
    tags=["tutor-referral-admin"],
    dependencies=[Depends(require_permission("admin.access"))],
)
metrics_api_router = APIRouter(
    prefix="/api/admin/tutor-referral",
    tags=["tutor-referral-admin"],
    dependencies=[Depends(require_permission("admin.access"))],
)


@metrics_admin_router.get("/metrics")
@metrics_api_router.get("/metrics")
def tutor_referral_metrics(
    admin: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.services.metrics_service import get_tutor_referral_metrics  # noqa: PLC0415
    scope = get_admin_tenant_scope(admin, db)
    return get_tutor_referral_metrics(db, scope)
