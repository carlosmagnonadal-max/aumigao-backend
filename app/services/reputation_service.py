from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.walk import Walk
from app.models.walker_profile import WalkerProfile
from app.models.walker_referral import WalkerReferral
from app.models.walker_reputation_snapshot import WalkerReputationSnapshot
from app.models.walker_review import WalkerReview
from app.schemas.walker_review import WalkerReviewCreate

COMPLETED_STATUSES = {"Finalizado", "Concluido", "Concluído", "finalizado", "completed", "finished"}
DEFAULT_WALKER_PHOTO = "https://images.unsplash.com/photo-1500648767791-00dcc994a43e?auto=format&fit=crop&w=400&q=85"


def clamp_score(value: float, min_value: float = 0.0, max_value: float = 100.0) -> float:
    return max(min_value, min(max_value, value))


def is_completed_walk(walk: Walk) -> bool:
    return (walk.status or "").strip() in COMPLETED_STATUSES


def walker_level(total_walks: int, rating_average: float, reviews_count: int) -> str:
    if reviews_count == 0:
        return "Iniciante"
    if total_walks >= 80 and rating_average >= 4.85:
        return "Elite Aumigao"
    if total_walks >= 30 and rating_average >= 4.7:
        return "Destaque"
    if total_walks >= 10 and rating_average >= 4.5:
        return "Confiavel"
    return "Iniciante"


def anonymized_tutor_name(user: User | None) -> str:
    if not user or not user.full_name:
        return "Tutor Aumigao"
    first_name = user.full_name.split()[0]
    return f"{first_name} A."


def public_review_payload(review: WalkerReview, db: Session) -> dict:
    tutor = db.get(User, review.tutor_id)
    return {
        "id": review.id,
        "walk_id": review.walk_id,
        "rating": review.rating,
        "comment": review.comment,
        "tutor_name": anonymized_tutor_name(tutor),
        "created_at": review.created_at,
    }


def admin_review_payload(review: WalkerReview) -> dict:
    return {
        "id": review.id,
        "walk_id": review.walk_id,
        "tutor_id": review.tutor_id,
        "walker_id": review.walker_id,
        "rating": review.rating,
        "comment": review.comment,
        "punctuality_rating": review.punctuality_rating,
        "care_rating": review.care_rating,
        "communication_rating": review.communication_rating,
        "is_flagged": review.is_flagged,
        "admin_notes": review.admin_notes,
        "created_at": review.created_at,
        "updated_at": review.updated_at,
    }


def walker_reviews_query(walker_id: str, db: Session):
    return db.query(WalkerReview).filter(WalkerReview.walker_id == walker_id).order_by(WalkerReview.created_at.desc())


def completed_walks_count(walker_id: str, db: Session) -> int:
    return db.query(Walk).filter(Walk.walker_id == walker_id, Walk.status.in_(COMPLETED_STATUSES)).count()


def calculate_rating_score(walker_id: str, db: Session) -> float:
    summary = reputation_summary(walker_id, db)
    if summary["reviews_count"] == 0:
        return 75.0
    return round(clamp_score((summary["rating_average"] / 5) * 100), 2)


def calculate_experience_score(walker_id: str, db: Session) -> float:
    total_walks = completed_walks_count(walker_id, db)
    if total_walks >= 80:
        return 100.0
    if total_walks >= 30:
        return 85.0
    if total_walks >= 10:
        return 70.0
    if total_walks >= 5:
        return 55.0
    return 40.0


def calculate_basic_behavior_score(walker_id: str, db: Session) -> dict:
    walks = db.query(Walk).filter(Walk.walker_id == walker_id).all()
    completed = [walk for walk in walks if (walk.status or "").strip() in COMPLETED_STATUSES]
    cancelled = [walk for walk in walks if (walk.status or "").strip().lower() == "cancelado"]
    active_days = len({walk.created_at.date().isoformat() for walk in completed if walk.created_at})

    if not walks:
        return {
            "behavior_score": 75.0,
            "acceptance_rate_score": 75.0,
            "cancellation_score": 75.0,
            "activity_score": 75.0,
            "cancellation_rate": 0.0,
        }

    acceptance_rate_score = 82.0
    cancellation_rate = len(cancelled) / max(1, len(walks))
    cancellation_score = clamp_score(100 - cancellation_rate * 100)
    activity_score = clamp_score(active_days * 12) if active_days else 75.0
    behavior_score = round(acceptance_rate_score * 0.40 + cancellation_score * 0.40 + activity_score * 0.20, 2)
    return {
        "behavior_score": behavior_score,
        "acceptance_rate_score": acceptance_rate_score,
        "cancellation_score": round(cancellation_score, 2),
        "activity_score": round(activity_score, 2),
        "cancellation_rate": round(cancellation_rate * 100, 2),
    }


def calculate_risk_penalty(walker_id: str, db: Session) -> float:
    flagged_reviews = db.query(WalkerReview).filter(WalkerReview.walker_id == walker_id, WalkerReview.is_flagged == True).count()
    return min(25.0, float(flagged_reviews * 5))


def calculate_hybrid_reputation_score(walker_id: str, db: Session) -> dict:
    rating_score = calculate_rating_score(walker_id, db)
    experience_score = calculate_experience_score(walker_id, db)
    behavior = calculate_basic_behavior_score(walker_id, db)
    risk_penalty = calculate_risk_penalty(walker_id, db)
    hybrid = round(clamp_score(rating_score * 0.70 + experience_score * 0.20 + behavior["behavior_score"] * 0.10 - risk_penalty), 2)
    risk_level = determine_risk_level(walker_id, db, hybrid_score=hybrid, behavior=behavior)
    return {
        "rating_score": rating_score,
        "experience_score": experience_score,
        "behavior_score": behavior["behavior_score"],
        "consistency_score": behavior["activity_score"],
        "recent_rating_score": None,
        "risk_penalty": risk_penalty,
        "hybrid_reputation_score": hybrid,
        "risk_level": risk_level,
        "behavior_details": behavior,
    }


def determine_risk_level(walker_id: str, db: Session, hybrid_score: float | None = None, behavior: dict | None = None) -> str:
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == walker_id).first()
    if profile and profile.status in {"suspended", "blocked"}:
        return "suspended"

    summary = reputation_summary(walker_id, db)
    rating = summary["rating_average"]
    reviews_count = summary["reviews_count"]
    behavior = behavior or calculate_basic_behavior_score(walker_id, db)
    cancellation_rate = float(behavior.get("cancellation_rate", 0))
    flagged_reviews = db.query(WalkerReview).filter(WalkerReview.walker_id == walker_id, WalkerReview.is_flagged == True).count()

    if flagged_reviews >= 3 or (reviews_count >= 3 and rating < 4.0):
        return "critical"
    if (reviews_count >= 3 and rating < 4.3) or cancellation_rate >= 25:
        return "risk"
    if (reviews_count >= 3 and rating < 4.6) or cancellation_rate >= 12:
        return "attention"
    if hybrid_score is not None and hybrid_score < 65:
        return "attention"
    return "normal"


def create_reputation_snapshot(walker_id: str, db: Session) -> WalkerReputationSnapshot:
    scores = calculate_hybrid_reputation_score(walker_id, db)
    snapshot = WalkerReputationSnapshot(
        id=str(uuid4()),
        walker_id=walker_id,
        rating_score=scores["rating_score"],
        experience_score=scores["experience_score"],
        behavior_score=scores["behavior_score"],
        consistency_score=scores["consistency_score"],
        recent_rating_score=scores["recent_rating_score"],
        risk_penalty=scores["risk_penalty"],
        hybrid_reputation_score=scores["hybrid_reputation_score"],
        risk_level=scores["risk_level"],
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot


def reputation_summary(walker_id: str, db: Session) -> dict:
    reviews = walker_reviews_query(walker_id, db).all()
    reviews_count = len(reviews)
    rating_average = round(sum(review.rating for review in reviews) / reviews_count, 2) if reviews_count else 0.0
    total_walks = completed_walks_count(walker_id, db)
    reputation_score = round((rating_average / 5) * 70 + min(total_walks, 80) / 80 * 15, 2) if reviews_count else None
    return {
        "rating_average": rating_average,
        "reviews_count": reviews_count,
        "total_walks": total_walks,
        "level": walker_level(total_walks, rating_average, reviews_count),
        "reputation_score": reputation_score,
        "acceptance_rate": None,
        "cancellation_rate": None,
    }


def get_walker_identity(walker_id: str, db: Session) -> dict:
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == walker_id).first()
    user = db.get(User, walker_id)
    name = (profile.full_name if profile else None) or (user.full_name if user else None) or "Passeador Aumigao"
    photo = (getattr(profile, "profile_photo_url", None) if profile else None) or (profile.selfie_url if profile else None) or ""
    return {
        "id": walker_id,
        "name": name,
        "photo": photo,
        "status": profile.status if profile else None,
        "member_since": profile.created_at if profile else (user.created_at if user else None),
        "bio": profile.bio if profile else None,
        "city": profile.city if profile else None,
        "neighborhood": profile.state if profile else None,
        "profile": profile,
        "user": user,
    }


def public_walker_profile(walker_id: str, db: Session, walker_kit: dict | None = None) -> dict:
    identity = get_walker_identity(walker_id, db)
    if not identity["profile"] and not identity["user"]:
        raise HTTPException(status_code=404, detail="Passeador nao encontrado")
    summary = reputation_summary(walker_id, db)
    recent_reviews = [public_review_payload(review, db) for review in walker_reviews_query(walker_id, db).limit(5).all()]
    return {
        **summary,
        "id": walker_id,
        "name": identity["name"],
        "photo": identity["photo"],
        "status": identity["status"],
        "recent_reviews": recent_reviews,
        "member_since": identity["member_since"],
        "bio": identity["bio"],
        "city": identity["city"],
        "neighborhood": identity["neighborhood"],
        "walker_kit": walker_kit,
        "empty_message": "Este passeador ainda esta comecando no Aumigao." if not recent_reviews else None,
    }


def motivational_message(summary: dict) -> str:
    rating = summary["rating_average"]
    reviews = summary["reviews_count"]
    if reviews == 0:
        return "Complete seus primeiros passeios para comecar a construir sua reputacao."
    if rating >= 4.8:
        return "Voce esta com uma excelente avaliacao. Continue mantendo esse cuidado."
    if rating >= 4.5:
        return "Seu desempenho esta muito bom. Pequenos detalhes ajudam a evoluir ainda mais."
    return "Revise os comentarios dos tutores e busque melhorar sua experiencia nos proximos passeios."


def walker_performance(user: User, db: Session) -> dict:
    summary = reputation_summary(user.id, db)
    recent_reviews = [public_review_payload(review, db) for review in walker_reviews_query(user.id, db).limit(5).all()]
    return {
        **summary,
        "recent_reviews": recent_reviews,
        "motivational_message": motivational_message(summary),
        "future_score_inputs": {
            "punctuality": None,
            "care": None,
            "communication": None,
            "acceptance_rate": None,
            "cancellation_rate": None,
            "reports": 0,
            "formula_preview": "70% avaliacao media + 15% volume + 10% aceite - 5% cancelamentos",
        },
    }


def create_walker_review(walk_id: str, payload: WalkerReviewCreate, user: User, db: Session) -> WalkerReview:
    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    if walk.tutor_id != user.id:
        raise HTTPException(status_code=403, detail="Apenas o tutor deste passeio pode avaliar.")
    if not walk.walker_id:
        raise HTTPException(status_code=400, detail="Este passeio ainda nao tem passeador vinculado.")
    if walk.walker_id == user.id:
        raise HTTPException(status_code=403, detail="Passeador nao pode avaliar a si mesmo.")
    if not is_completed_walk(walk):
        raise HTTPException(status_code=400, detail="A avaliacao so fica disponivel apos passeio finalizado.")
    exists = db.query(WalkerReview).filter(WalkerReview.walk_id == walk.id).first()
    if exists:
        raise HTTPException(status_code=409, detail="Este passeio ja recebeu avaliacao.")

    review = WalkerReview(
        id=str(uuid4()),
        walk_id=walk.id,
        tutor_id=walk.tutor_id,
        walker_id=walk.walker_id,
        rating=payload.rating,
        comment=(payload.comment or "").strip() or None,
    )
    db.add(review)
    db.commit()
    db.refresh(review)
    update_referred_walker_performance(review.walker_id, db)
    create_reputation_snapshot(review.walker_id, db)
    return review


def update_referred_walker_performance(walker_id: str, db: Session) -> None:
    referral = db.query(WalkerReferral).filter(WalkerReferral.referred_user_id == walker_id).first()
    if not referral:
        return
    summary = reputation_summary(walker_id, db)
    referral.completed_walks_count = summary["total_walks"]
    referral.average_rating = summary["rating_average"] or None
    if summary["reviews_count"] == 0:
        referral.performance_status = "neutral"
    elif summary["rating_average"] >= 4.85:
        referral.performance_status = "excellent"
    elif summary["rating_average"] >= 4.6:
        referral.performance_status = "good"
    elif summary["rating_average"] >= 4.2:
        referral.performance_status = "warning"
    else:
        referral.performance_status = "bad"
    db.commit()


def admin_walker_reputation(walker_id: str, db: Session, walker_kit: dict | None = None) -> dict:
    profile_payload = public_walker_profile(walker_id, db, walker_kit)
    reviews = walker_reviews_query(walker_id, db).all()
    referral = db.query(WalkerReferral).filter(WalkerReferral.referred_user_id == walker_id).first()
    return {
        **profile_payload,
        "reviews": [admin_review_payload(review) for review in reviews],
        "flagged_reviews_count": len([review for review in reviews if review.is_flagged]),
        "referral_performance": None if not referral else {
            "referral_id": referral.id,
            "referrer_user_id": referral.referrer_user_id,
            "status": referral.status,
            "performance_status": referral.performance_status,
            "completed_walks_count": referral.completed_walks_count,
            "average_rating": referral.average_rating,
        },
        "future_score_inputs": {
            "punctuality": None,
            "care": None,
            "communication": None,
            "acceptance_rate": None,
            "cancellation_rate": None,
            "reports": 0,
        },
    }


def flag_review(review_id: str, is_flagged: bool, admin_notes: str | None, db: Session) -> WalkerReview:
    review = db.get(WalkerReview, review_id)
    if not review:
        raise HTTPException(status_code=404, detail="Avaliacao nao encontrada")
    review.is_flagged = is_flagged
    review.admin_notes = (admin_notes or "").strip() or None
    db.commit()
    db.refresh(review)
    create_reputation_snapshot(review.walker_id, db)
    return review
