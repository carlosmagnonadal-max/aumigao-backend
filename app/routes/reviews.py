from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user, require_admin
from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.schemas.walker_review import (
    AdminWalkerReputationListResponse,
    AdminWalkerReputationResponse,
    WalkerPerformanceResponse,
    WalkerReviewCreate,
    WalkerReviewFlagUpdate,
    WalkerReviewListResponse,
    WalkerReviewResponse,
    PublicWalkerProfileResponse,
)
from app.services.reputation_service import (
    admin_walker_reputation,
    create_walker_review,
    flag_review,
    public_review_payload,
    public_walker_profile,
    reputation_summary,
    walker_performance,
    walker_reviews_query,
)

walks_router = APIRouter(prefix="/walks", tags=["reviews"])
api_walks_router = APIRouter(prefix="/api/walks", tags=["reviews"])
walkers_router = APIRouter(prefix="/walkers", tags=["walkers"])
api_walkers_router = APIRouter(prefix="/api/walkers", tags=["walkers"])
walker_router = APIRouter(prefix="/walker", tags=["walker"])
api_walker_router = APIRouter(prefix="/api/walker", tags=["walker"])
admin_router = APIRouter(prefix="/admin", tags=["admin-reputation"], dependencies=[Depends(require_admin)])
api_admin_router = APIRouter(prefix="/api/admin", tags=["admin-reputation"], dependencies=[Depends(require_admin)])


@walks_router.post("/{walk_id}/review", response_model=WalkerReviewResponse)
@api_walks_router.post("/{walk_id}/review", response_model=WalkerReviewResponse)
def create_review_endpoint(walk_id: str, payload: WalkerReviewCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return create_walker_review(walk_id, payload, user, db)


@walkers_router.get("/{walker_id}/reviews", response_model=WalkerReviewListResponse)
@api_walkers_router.get("/{walker_id}/reviews", response_model=WalkerReviewListResponse)
def walker_reviews_endpoint(walker_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    reviews = walker_reviews_query(walker_id, db).all()
    return {"items": [public_review_payload(review, db) for review in reviews], "total": len(reviews)}


@walkers_router.get("/{walker_id}/profile", response_model=PublicWalkerProfileResponse)
@api_walkers_router.get("/{walker_id}/profile", response_model=PublicWalkerProfileResponse)
def walker_public_profile_endpoint(walker_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return public_walker_profile(walker_id, db)


@walker_router.get("/me/performance", response_model=WalkerPerformanceResponse)
@api_walker_router.get("/me/performance", response_model=WalkerPerformanceResponse)
def my_walker_performance_endpoint(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return walker_performance(user, db)


@admin_router.get("/walkers/reputation", response_model=AdminWalkerReputationListResponse)
@api_admin_router.get("/walkers/reputation", response_model=AdminWalkerReputationListResponse)
def admin_walker_reputation_list(
    sort: str = Query("rating"),
    status: str | None = None,
    db: Session = Depends(get_db),
):
    profiles = db.query(WalkerProfile).all()
    rows = []
    for profile in profiles:
        if status and profile.status != status:
            continue
        summary = reputation_summary(profile.user_id, db)
        reviews = walker_reviews_query(profile.user_id, db).all()
        rows.append({
            **summary,
            "walker_id": profile.user_id,
            "name": profile.full_name or "Passeador Aumigao",
            "status": profile.status,
            "photo": profile.selfie_url,
            "flagged_reviews_count": len([review for review in reviews if review.is_flagged]),
        })

    if sort == "worst":
        rows.sort(key=lambda item: item["rating_average"])
    elif sort == "reviews":
        rows.sort(key=lambda item: item["reviews_count"], reverse=True)
    elif sort == "walks":
        rows.sort(key=lambda item: item["total_walks"], reverse=True)
    elif sort == "flagged":
        rows.sort(key=lambda item: item["flagged_reviews_count"], reverse=True)
    else:
        rows.sort(key=lambda item: item["rating_average"], reverse=True)
    return {"items": rows, "total": len(rows)}


@admin_router.get("/walkers/{walker_id}/reputation", response_model=AdminWalkerReputationResponse)
@api_admin_router.get("/walkers/{walker_id}/reputation", response_model=AdminWalkerReputationResponse)
def admin_walker_reputation_endpoint(walker_id: str, db: Session = Depends(get_db)):
    return admin_walker_reputation(walker_id, db)


@admin_router.patch("/reviews/{review_id}/flag", response_model=WalkerReviewResponse)
@api_admin_router.patch("/reviews/{review_id}/flag", response_model=WalkerReviewResponse)
def admin_flag_review_endpoint(review_id: str, payload: WalkerReviewFlagUpdate, db: Session = Depends(get_db)):
    return flag_review(review_id, payload.is_flagged, payload.admin_notes, db)
