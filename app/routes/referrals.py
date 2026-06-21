from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user, require_admin
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import get_admin_tenant_scope
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walker_referral import WalkerReferral
from app.schemas.metrics import ReferralMetricsResponse
from app.schemas.walker_referral import (
    AdminWalkerReferralListResponse,
    AdminWalkerReferralResponse,
    AdminWalkerReferralStatusUpdate,
    WalkerReferralCreate,
    WalkerReferralLinkUser,
    WalkerReferralListResponse,
    WalkerReferralResponse,
    WalkerReferralSummary,
    WalkerReferralValidateCode,
)
from app.services.metrics_service import get_referral_metrics
from app.services.tenant_plan_service import tenant_feature_enabled
from app.services.walker_referrals import create_walker_referral, link_referral_to_user, update_referral_status, validate_referral_code


def _assert_referral_feature(user: User, db: Session, feature_key: str) -> None:
    """Gate de referral por tenant. O AppSetting global continua como kill-switch adicional."""
    tenant_id = user.tenant_id
    if not tenant_id:
        return
    tenant = db.get(Tenant, tenant_id)
    if tenant and not tenant_feature_enabled(tenant, db, feature_key):
        raise HTTPException(status_code=403, detail="Programa de indicações não está habilitado para este tenant.")

router = APIRouter(prefix="/referrals", tags=["referrals"])
api_router = APIRouter(prefix="/api/referrals", tags=["referrals"])
admin_router = APIRouter(prefix="/admin/referrals", tags=["admin-referrals"], dependencies=[Depends(require_permission("referrals.read"))])
api_admin_router = APIRouter(prefix="/api/admin/referrals", tags=["admin-referrals"], dependencies=[Depends(require_permission("referrals.read"))])


def _admin_payload(referral: WalkerReferral, db: Session) -> AdminWalkerReferralResponse:
    referrer = db.get(User, referral.referrer_user_id)
    referred = db.get(User, referral.referred_user_id) if referral.referred_user_id else None
    data = WalkerReferralResponse.model_validate(referral).model_dump()
    return AdminWalkerReferralResponse(
        **data,
        referrer_name=(referrer.full_name if referrer else None) or (referrer.email if referrer else "") or "Usuario",
        referrer_role=(referrer.role if referrer else "") or "",
        referred_user_name=(referred.full_name if referred else None) or (referred.email if referred else None),
    )


@router.post("/walkers", response_model=WalkerReferralResponse)
@api_router.post("/walkers", response_model=WalkerReferralResponse)
def create_walker_referral_endpoint(payload: WalkerReferralCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _assert_referral_feature(user, db, "walker_referrals")
    return create_walker_referral(payload, user, db)


@router.get("/walkers/my", response_model=WalkerReferralListResponse)
@api_router.get("/walkers/my", response_model=WalkerReferralListResponse)
def my_walker_referrals(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    items = (
        db.query(WalkerReferral)
        .filter(WalkerReferral.referrer_user_id == user.id)
        .order_by(WalkerReferral.created_at.desc())
        .all()
    )
    return {"items": items, "total": len(items)}


@router.get("/walkers/my/summary", response_model=WalkerReferralSummary)
@api_router.get("/walkers/my/summary", response_model=WalkerReferralSummary)
def my_walker_referral_summary(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    items = db.query(WalkerReferral).filter(WalkerReferral.referrer_user_id == user.id).all()
    return {
        "total": len(items),
        "pending": len([item for item in items if item.status in {"pending", "invited", "registered", "under_review"}]),
        "approved": len([item for item in items if item.status == "approved"]),
        "converted": len([item for item in items if item.status == "converted"]),
        "eligible_reward": sum(float(item.reward_amount or 0) for item in items if item.reward_status == "eligible"),
        "paid_reward": sum(float(item.reward_amount or 0) for item in items if item.reward_status == "paid"),
    }


@router.post("/walkers/validate-code")
@api_router.post("/walkers/validate-code")
def validate_walker_referral_code(payload: WalkerReferralValidateCode, db: Session = Depends(get_db)):
    referral = validate_referral_code(payload.referral_code, db)
    return {
        "valid": True,
        "referral_id": referral.id,
        "referral_code": referral.referral_code,
        "referred_name": referral.referred_name,
        "city": referral.city,
        "neighborhood": referral.neighborhood,
    }


@router.patch("/walkers/{referral_id}/link-user", response_model=WalkerReferralResponse)
@api_router.patch("/walkers/{referral_id}/link-user", response_model=WalkerReferralResponse)
def link_walker_referral_user(referral_id: str, payload: WalkerReferralLinkUser, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    referral = db.get(WalkerReferral, referral_id)
    if not referral:
        raise HTTPException(status_code=404, detail="Indicacao nao encontrada.")
    code = payload.referral_code or referral.referral_code
    if code != referral.referral_code:
        raise HTTPException(status_code=409, detail="Codigo nao corresponde a indicacao.")
    return link_referral_to_user(code, user, db)


@admin_router.get("/metrics", response_model=ReferralMetricsResponse)
@api_admin_router.get("/metrics", response_model=ReferralMetricsResponse)
def admin_referral_metrics(
    admin: User = Depends(require_permission("referrals.read")),
    db: Session = Depends(get_db),
):
    """Métricas de indicações de passeador: totais, by_status, ativadas, recompensas e série semanal.

    WalkerReferral não possui tenant_id — dados são globais independente do scope.
    """
    scope = get_admin_tenant_scope(admin, db)
    data = get_referral_metrics(db, scope)
    return ReferralMetricsResponse(**data)


@admin_router.get("/walkers", response_model=AdminWalkerReferralListResponse)
@api_admin_router.get("/walkers", response_model=AdminWalkerReferralListResponse)
def admin_walker_referrals(
    status: str | None = Query(default=None),
    search: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    query = db.query(WalkerReferral)
    if status and status != "all":
        query = query.filter(WalkerReferral.status == status)
    if search:
        like = f"%{search.strip()}%"
        query = query.filter(
            (WalkerReferral.referred_name.ilike(like))
            | (WalkerReferral.referred_phone.ilike(like))
            | (WalkerReferral.city.ilike(like))
            | (WalkerReferral.neighborhood.ilike(like))
        )
    rows = query.order_by(WalkerReferral.created_at.desc()).all()
    return {"items": [_admin_payload(row, db) for row in rows], "total": len(rows)}


@admin_router.patch("/walkers/{referral_id}/status", response_model=AdminWalkerReferralResponse)
@api_admin_router.patch("/walkers/{referral_id}/status", response_model=AdminWalkerReferralResponse)
def admin_update_walker_referral_status(referral_id: str, payload: AdminWalkerReferralStatusUpdate, db: Session = Depends(get_db)):
    referral = db.get(WalkerReferral, referral_id)
    if not referral:
        raise HTTPException(status_code=404, detail="Indicacao nao encontrada.")
    updated = update_referral_status(referral, payload, db)
    return _admin_payload(updated, db)
