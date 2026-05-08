from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.walker_boost import WalkerBoost
from app.models.walker_profile import WalkerProfile
from app.services.reputation_service import determine_risk_level, reputation_summary

MAX_BOOST_SCORE = 5


def active_boost_for_walker(walker_id: str, db: Session) -> WalkerBoost | None:
    now = datetime.utcnow()
    boost = (
        db.query(WalkerBoost)
        .filter(WalkerBoost.walker_id == walker_id)
        .order_by(WalkerBoost.updated_at.desc())
        .first()
    )
    if not boost or not boost.boost_enabled:
        return None
    if boost.boost_status != "active":
        return None
    if boost.boost_start_at and boost.boost_start_at > now:
        return None
    if boost.boost_end_at and boost.boost_end_at < now:
        boost.boost_status = "expired"
        boost.boost_enabled = False
        db.commit()
        return None
    return boost


def validate_boost_eligibility(profile: WalkerProfile | None, walker_id: str, db: Session) -> tuple[bool, str]:
    if not profile:
        return False, "Perfil nao encontrado"
    if profile.status != "approved":
        return False, "Boost apenas para passeador approved"
    risk_level = determine_risk_level(walker_id, db)
    if risk_level in {"critical", "suspended"}:
        return False, "Boost bloqueado para revisao de qualidade"
    summary = reputation_summary(walker_id, db)
    if summary["reviews_count"] > 0 and summary["rating_average"] < 4.5:
        return False, "Avaliacao minima para boost nao atingida"
    return True, "Elegivel para boost controlado"


def boost_score_for_walker(profile: WalkerProfile | None, walker_id: str, db: Session) -> float:
    can_apply, _ = validate_boost_eligibility(profile, walker_id, db)
    if not can_apply:
        return 0.0
    boost = active_boost_for_walker(walker_id, db)
    if not boost:
        return 0.0
    return float(max(0, min(MAX_BOOST_SCORE, boost.boost_score or 0)))


def get_or_create_boost(walker_id: str, db: Session) -> WalkerBoost:
    boost = db.query(WalkerBoost).filter(WalkerBoost.walker_id == walker_id).order_by(WalkerBoost.updated_at.desc()).first()
    if boost:
        return boost
    boost = WalkerBoost(id=str(uuid4()), walker_id=walker_id, boost_enabled=False, boost_score=0, boost_status="inactive")
    db.add(boost)
    db.commit()
    db.refresh(boost)
    return boost


def update_boost(walker_id: str, payload: dict, db: Session) -> WalkerBoost:
    boost = get_or_create_boost(walker_id, db)
    boost.boost_enabled = bool(payload.get("boost_enabled"))
    boost.boost_type = payload.get("boost_type") or ("admin_boost" if boost.boost_enabled else None)
    boost.boost_score = max(0, min(MAX_BOOST_SCORE, int(payload.get("boost_score") or 0)))
    boost.boost_start_at = payload.get("boost_start_at") or (datetime.utcnow() if boost.boost_enabled else None)
    boost.boost_end_at = payload.get("boost_end_at") or (datetime.utcnow() + timedelta(days=7) if boost.boost_enabled else None)
    boost.boost_reason = (payload.get("boost_reason") or "").strip() or None
    boost.boost_status = payload.get("boost_status") or ("active" if boost.boost_enabled else "inactive")
    if boost.boost_enabled and boost.boost_status == "inactive":
        boost.boost_status = "active"
    db.commit()
    db.refresh(boost)
    return boost
