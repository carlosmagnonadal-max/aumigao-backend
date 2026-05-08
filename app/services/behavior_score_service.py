from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.walk import Walk
from app.models.walker_review import WalkerReview
from app.services.reputation_service import COMPLETED_STATUSES


def clamp(value: float, min_value: float = 0, max_value: float = 100) -> float:
    return max(min_value, min(max_value, value))


def get_behavior_score(walker_id: str, db: Session) -> dict:
    walks = db.query(Walk).filter(Walk.walker_id == walker_id).all()
    completed = [walk for walk in walks if (walk.status or "").strip() in COMPLETED_STATUSES]
    cancelled = [walk for walk in walks if (walk.status or "").strip().lower() == "cancelado"]
    active_days = len({walk.created_at.date().isoformat() for walk in completed if walk.created_at})
    recent_cutoff = datetime.utcnow() - timedelta(days=45)
    recent_reviews = (
        db.query(WalkerReview)
        .filter(WalkerReview.walker_id == walker_id, WalkerReview.created_at >= recent_cutoff)
        .all()
    )

    acceptance_rate_score = 82.0 if walks else 75.0
    cancellation_rate = len(cancelled) / max(1, len(walks))
    cancellation_score = clamp(100 - cancellation_rate * 100)
    response_time_score = 84.0 if completed else 75.0
    recent_rating_score = (
        clamp((sum(review.rating for review in recent_reviews) / len(recent_reviews)) / 5 * 100)
        if recent_reviews
        else 75.0
    )
    consistency_score = clamp(active_days * 8) if active_days else 75.0

    behavior_score = round(
        acceptance_rate_score * 0.30
        + cancellation_score * 0.25
        + response_time_score * 0.20
        + recent_rating_score * 0.15
        + consistency_score * 0.10,
        2,
    )

    return {
        "behavior_score": behavior_score,
        "acceptance_rate_score": acceptance_rate_score,
        "cancellation_score": round(cancellation_score, 2),
        "response_time_score": response_time_score,
        "recent_rating_score": round(recent_rating_score, 2),
        "consistency_score": round(consistency_score, 2),
    }
