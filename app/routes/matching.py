from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user, require_admin
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import get_admin_tenant_scope
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.services.tenant_plan_service import tenant_feature_enabled
from app.schemas.matching import (
    MatchingDebugResponse,
    MatchingResponse,
    MatchingWalkerRequest,
    WalkerBoostListResponse,
    WalkerBoostResponse,
    WalkerBoostUpdate,
)
from app.services.boost_service import get_or_create_boost, update_boost, validate_boost_eligibility
from app.services.matching_service import rank_walkers
from app.services.reputation_service import get_walker_identity, reputation_summary

router = APIRouter(prefix="/matching", tags=["matching"])
api_router = APIRouter(prefix="/api/matching", tags=["matching"])
admin_router = APIRouter(prefix="/admin/matching", tags=["admin-matching"], dependencies=[Depends(require_permission("matching.read"))])
api_admin_router = APIRouter(prefix="/api/admin/matching", tags=["admin-matching"], dependencies=[Depends(require_permission("matching.read"))])


@router.post("/walkers", response_model=MatchingResponse)
@api_router.post("/walkers", response_model=MatchingResponse)
def match_walkers(payload: MatchingWalkerRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # C11/mt-MT3: restringe a vitrine ao pool da rede do tenant do solicitante
    # (mesma fronteira da alocação vinculante) — não vaza passeadores cross-tenant.
    return rank_walkers(payload, db, debug=False, tenant_id=getattr(user, "tenant_id", None))


def boost_response_payload(profile: WalkerProfile, db: Session) -> dict:
    boost = get_or_create_boost(profile.user_id, db)
    summary = reputation_summary(profile.user_id, db)
    identity = get_walker_identity(profile.user_id, db)
    can_apply, reason = validate_boost_eligibility(profile, profile.user_id, db)
    return {
        "walker_id": profile.user_id,
        "walker_name": identity["name"],
        "status": profile.status,
        "rating_average": summary["rating_average"],
        "reviews_count": summary["reviews_count"],
        "total_walks": summary["total_walks"],
        "boost_enabled": boost.boost_enabled,
        "boost_type": boost.boost_type,
        "boost_score": boost.boost_score,
        "boost_start_at": boost.boost_start_at,
        "boost_end_at": boost.boost_end_at,
        "boost_reason": boost.boost_reason,
        "boost_status": boost.boost_status,
        "can_apply_boost": can_apply,
        "eligibility_reason": reason,
    }


@admin_router.get("/debug", response_model=MatchingDebugResponse)
@api_admin_router.get("/debug", response_model=MatchingDebugResponse)
def matching_debug(
    city: str | None = Query(None),
    neighborhood: str | None = Query(None),
    scheduled_at: str | None = Query(None),
    duration_minutes: int = Query(45, ge=15, le=180),
    db: Session = Depends(get_db),
):
    payload = MatchingWalkerRequest(
        city=city,
        neighborhood=neighborhood,
        scheduled_at=scheduled_at,
        duration_minutes=duration_minutes,
    )
    return rank_walkers(payload, db, debug=True)


@admin_router.get("/boosts", response_model=WalkerBoostListResponse)
@api_admin_router.get("/boosts", response_model=WalkerBoostListResponse)
def list_walker_boosts(status: str | None = Query(None), db: Session = Depends(get_db)):
    profiles = db.query(WalkerProfile).all()
    rows = [boost_response_payload(profile, db) for profile in profiles if not status or profile.status == status]
    rows.sort(key=lambda item: (item["boost_enabled"], item["rating_average"], item["total_walks"]), reverse=True)
    return {"items": rows, "total": len(rows)}


@admin_router.patch("/boosts/{walker_id}", response_model=WalkerBoostResponse)
@api_admin_router.patch("/boosts/{walker_id}", response_model=WalkerBoostResponse)
def update_walker_boost(walker_id: str, payload: WalkerBoostUpdate, admin: User = Depends(require_permission("matching.read")), db: Session = Depends(get_db)):
    # Gate walker_boosts por tenant do admin (super_admin global não aplica gate).
    scope = get_admin_tenant_scope(admin)
    if scope.tenant_id:
        _boost_tenant = db.get(Tenant, scope.tenant_id)
        if _boost_tenant and not tenant_feature_enabled(_boost_tenant, db, "walker_boosts"):
            raise HTTPException(status_code=403, detail="Boosts de passeador não estão habilitados para este tenant.")
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == walker_id).first()
    if not profile:
        profile = WalkerProfile(
            id=f"profile-{walker_id}",
            user_id=walker_id,
            full_name="Passeador Aumigao",
            status="pending",
            created_at=datetime.utcnow(),
        )
        db.add(profile)
        db.commit()
        db.refresh(profile)

    update_boost(walker_id, payload.model_dump(), db)
    return boost_response_payload(profile, db)
