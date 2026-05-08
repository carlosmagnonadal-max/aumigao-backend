from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.walker_incentive import WalkerIncentive
from app.models.walker_profile import WalkerProfile
from app.services.reputation_service import calculate_hybrid_reputation_score, reputation_summary
from app.services.weekly_mission_service import get_walker_mission_summary


def incentive_payload(incentive: WalkerIncentive) -> dict:
    return {
        "id": incentive.id,
        "walker_id": incentive.walker_id,
        "incentive_type": incentive.incentive_type,
        "title": incentive.title,
        "description": incentive.description,
        "source": incentive.source,
        "status": incentive.status,
        "visibility_effect": incentive.visibility_effect,
        "created_at": incentive.created_at,
        "updated_at": incentive.updated_at,
        "expires_at": incentive.expires_at,
        "granted_at": incentive.granted_at,
        "revoked_at": incentive.revoked_at,
        "admin_notes": incentive.admin_notes,
    }


def expire_incentives(walker_id: str, db: Session) -> None:
    now = datetime.utcnow()
    rows = (
        db.query(WalkerIncentive)
        .filter(WalkerIncentive.walker_id == walker_id, WalkerIncentive.status == "active", WalkerIncentive.expires_at != None, WalkerIncentive.expires_at < now)
        .all()
    )
    for row in rows:
        row.status = "expired"
    if rows:
        db.commit()


def get_active_incentives(walker_id: str, db: Session) -> list[WalkerIncentive]:
    expire_incentives(walker_id, db)
    return (
        db.query(WalkerIncentive)
        .filter(WalkerIncentive.walker_id == walker_id, WalkerIncentive.status == "active")
        .order_by(WalkerIncentive.created_at.desc())
        .all()
    )


def grant_incentive(
    walker_id: str,
    incentive_type: str,
    title: str,
    description: str,
    source: str,
    db: Session,
    visibility_effect: str = "none",
    expires_at: datetime | None = None,
    admin_notes: str | None = None,
) -> WalkerIncentive:
    existing = (
        db.query(WalkerIncentive)
        .filter(
            WalkerIncentive.walker_id == walker_id,
            WalkerIncentive.incentive_type == incentive_type,
            WalkerIncentive.title == title,
            WalkerIncentive.status.in_(["active", "pending"]),
        )
        .first()
    )
    if existing:
        return existing

    incentive = WalkerIncentive(
        id=str(uuid4()),
        walker_id=walker_id,
        incentive_type=incentive_type,
        title=title,
        description=description,
        source=source,
        status="active",
        visibility_effect=visibility_effect,
        expires_at=expires_at or datetime.utcnow() + timedelta(days=7),
        granted_at=datetime.utcnow(),
        admin_notes=admin_notes,
    )
    db.add(incentive)
    db.commit()
    db.refresh(incentive)
    return incentive


def revoke_incentive(incentive_id: str, db: Session, admin_notes: str | None = None) -> WalkerIncentive:
    incentive = db.get(WalkerIncentive, incentive_id)
    if not incentive:
        raise HTTPException(status_code=404, detail="Incentivo nao encontrado")
    incentive.status = "revoked"
    incentive.revoked_at = datetime.utcnow()
    incentive.admin_notes = admin_notes or incentive.admin_notes
    db.commit()
    db.refresh(incentive)
    return incentive


def evaluate_incentives(walker_id: str, db: Session) -> list[WalkerIncentive]:
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == walker_id).first()
    if not profile or profile.status != "approved":
        return get_active_incentives(walker_id, db)

    scores = calculate_hybrid_reputation_score(walker_id, db)
    if scores["risk_level"] in {"risk", "critical", "suspended"}:
        return get_active_incentives(walker_id, db)

    summary = reputation_summary(walker_id, db)
    mission_summary = get_walker_mission_summary(walker_id, db)

    if summary["reviews_count"] >= 5 and summary["rating_average"] >= 4.8:
        grant_incentive(
            walker_id,
            "badge",
            "Passeador bem avaliado",
            "Reconhecimento por manter uma avaliacao alta com volume minimo de avaliacoes.",
            "reputation",
            db,
            visibility_effect="low",
        )

    if mission_summary.get("completed_missions", 0) >= 3:
        grant_incentive(
            walker_id,
            "recognition",
            "Evolucao consistente",
            "Voce concluiu missoes leves que fortalecem sua presenca profissional.",
            "missions",
            db,
            visibility_effect="low",
        )

    if scores["hybrid_reputation_score"] >= 88 and scores["risk_level"] == "normal":
        grant_incentive(
            walker_id,
            "visibility_boost",
            "Destaque da semana",
            "Elegibilidade para destaque controlado por qualidade real. Sem relacao com gorjetas.",
            "performance",
            db,
            visibility_effect="medium",
        )

    return get_active_incentives(walker_id, db)


def list_incentives(walker_id: str, db: Session) -> list[WalkerIncentive]:
    expire_incentives(walker_id, db)
    return db.query(WalkerIncentive).filter(WalkerIncentive.walker_id == walker_id).order_by(WalkerIncentive.created_at.desc()).all()
