from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.tip_integrity_flag import TipIntegrityFlag
from app.models.user import User
from app.models.walker_incentive import WalkerIncentive
from app.models.walker_monitoring_alert import WalkerMonitoringAlert
from app.models.walker_profile import WalkerProfile
from app.models.walker_reputation_snapshot import WalkerReputationSnapshot
from app.models.walker_review import WalkerReview
from app.services.incentive_engine_service import evaluate_incentives, incentive_payload, list_incentives
from app.services.monitoring_service import alert_payload, evaluate_monitoring_alerts, open_alerts
from app.services.recovery_service import active_recovery_plan, build_recommendations, get_or_create_recovery_plan, recovery_payload
from app.services.reputation_service import (
    admin_review_payload,
    calculate_basic_behavior_score,
    calculate_hybrid_reputation_score,
    create_reputation_snapshot,
    get_walker_identity,
    reputation_summary,
)
from app.services.tip_integrity_service import TIP_REPUTATION_POLICY, evaluate_tip_patterns, tip_flag_payload


def ensure_walker_user(user: User, db: Session) -> WalkerProfile:
    if user.role not in {"walker", "passeador"}:
        raise HTTPException(status_code=403, detail="Apenas passeadores podem acessar estes indicadores.")
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Perfil de passeador nao encontrado.")
    return profile


def score_breakdown_payload(scores: dict) -> dict:
    return {
        "rating_score": scores["rating_score"],
        "experience_score": scores["experience_score"],
        "behavior_score": scores["behavior_score"],
        "consistency_score": scores.get("consistency_score"),
        "recent_rating_score": scores.get("recent_rating_score"),
        "risk_penalty": scores["risk_penalty"],
        "hybrid_reputation_score": scores["hybrid_reputation_score"],
        "risk_level": scores["risk_level"],
    }


def snapshot_payload(snapshot: WalkerReputationSnapshot) -> dict:
    return {
        "rating_score": snapshot.rating_score,
        "experience_score": snapshot.experience_score,
        "behavior_score": snapshot.behavior_score,
        "consistency_score": snapshot.consistency_score,
        "recent_rating_score": snapshot.recent_rating_score,
        "risk_penalty": snapshot.risk_penalty,
        "hybrid_reputation_score": snapshot.hybrid_reputation_score,
        "risk_level": snapshot.risk_level,
    }


def motivational_message(risk_level: str, reviews_count: int) -> str:
    if reviews_count == 0:
        return "Complete seus primeiros passeios para comecar a construir sua reputacao."
    if risk_level == "normal":
        return "Seu desempenho esta saudavel. Continue mantendo bons passeios."
    if risk_level == "attention":
        return "Alguns indicadores merecem atencao. Pequenos ajustes podem fortalecer sua reputacao."
    if risk_level == "risk":
        return "Criamos recomendacoes para ajudar voce a recuperar sua reputacao nos proximos passeios."
    if risk_level == "critical":
        return "Sua conta esta em revisao pela equipe Aumigao. Acompanhe as orientacoes disponiveis."
    return "Acompanhe sua evolucao e mantenha bons passeios."


def get_walker_reputation_health(user: User, db: Session) -> dict:
    ensure_walker_user(user, db)
    scores = calculate_hybrid_reputation_score(user.id, db)
    snapshot = create_reputation_snapshot(user.id, db)
    alerts = evaluate_monitoring_alerts(user.id, db)
    incentives = evaluate_incentives(user.id, db)
    recovery_plan = get_or_create_recovery_plan(user.id, db)
    summary = reputation_summary(user.id, db)

    return {
        **summary,
        "hybrid_reputation_score": snapshot.hybrid_reputation_score,
        "risk_level": snapshot.risk_level,
        "active_incentives": [incentive_payload(incentive) for incentive in incentives],
        "active_recovery_plan": recovery_payload(recovery_plan) if recovery_plan else None,
        "recommendations": build_recommendations(user.id, db),
        "motivational_message": motivational_message(scores["risk_level"], summary["reviews_count"]),
        "score_breakdown": score_breakdown_payload(scores),
        "tip_policy": TIP_REPUTATION_POLICY,
    }


def walker_quality_item(profile: WalkerProfile, db: Session) -> dict:
    summary = reputation_summary(profile.user_id, db)
    scores = calculate_hybrid_reputation_score(profile.user_id, db)
    behavior = calculate_basic_behavior_score(profile.user_id, db)
    identity = get_walker_identity(profile.user_id, db)
    alerts = open_alerts(profile.user_id, db)
    recovery_plan = active_recovery_plan(profile.user_id, db)
    incentives = evaluate_incentives(profile.user_id, db)
    tip_flags = evaluate_tip_patterns(profile.user_id, db)
    return {
        "walker_id": profile.user_id,
        "name": identity["name"],
        "status": profile.status,
        "rating_average": summary["rating_average"],
        "reviews_count": summary["reviews_count"],
        "total_walks": summary["total_walks"],
        "level": summary["level"],
        "hybrid_reputation_score": scores["hybrid_reputation_score"],
        "risk_level": scores["risk_level"],
        "open_alerts_count": len(alerts),
        "active_incentives_count": len(incentives),
        "active_recovery_plan": bool(recovery_plan),
        "tip_flags_count": len([flag for flag in tip_flags if flag.status == "open"]),
        "cancellation_rate": behavior["cancellation_rate"],
    }


def get_quality_dashboard(
    db: Session,
    risk_level: str | None = None,
    status: str | None = None,
    has_open_alerts: bool | None = None,
    has_recovery_plan: bool | None = None,
    has_tip_flags: bool | None = None,
) -> dict:
    profiles = db.query(WalkerProfile).all()
    rows = []
    for profile in profiles:
        item = walker_quality_item(profile, db)
        if risk_level and risk_level != "all" and item["risk_level"] != risk_level:
            continue
        if status and status != "all" and item["status"] != status:
            continue
        if has_open_alerts is not None and bool(item["open_alerts_count"]) != has_open_alerts:
            continue
        if has_recovery_plan is not None and item["active_recovery_plan"] != has_recovery_plan:
            continue
        if has_tip_flags is not None and bool(item["tip_flags_count"]) != has_tip_flags:
            continue
        rows.append(item)

    rows.sort(key=lambda item: (item["risk_level"] != "normal", item["open_alerts_count"], -item["hybrid_reputation_score"]), reverse=True)
    return {"items": rows, "total": len(rows)}


def get_walker_quality_detail(walker_id: str, db: Session) -> dict:
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == walker_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Passeador nao encontrado")

    scores = calculate_hybrid_reputation_score(walker_id, db)
    create_reputation_snapshot(walker_id, db)
    evaluate_monitoring_alerts(walker_id, db)
    evaluate_incentives(walker_id, db)
    recovery_plan = get_or_create_recovery_plan(walker_id, db)
    snapshots = (
        db.query(WalkerReputationSnapshot)
        .filter(WalkerReputationSnapshot.walker_id == walker_id)
        .order_by(WalkerReputationSnapshot.calculated_at.desc())
        .limit(10)
        .all()
    )
    reviews = db.query(WalkerReview).filter(WalkerReview.walker_id == walker_id).order_by(WalkerReview.created_at.desc()).limit(20).all()
    alerts = db.query(WalkerMonitoringAlert).filter(WalkerMonitoringAlert.walker_id == walker_id).order_by(WalkerMonitoringAlert.created_at.desc()).all()
    incentives = list_incentives(walker_id, db)
    tip_flags = db.query(TipIntegrityFlag).filter(TipIntegrityFlag.walker_id == walker_id).order_by(TipIntegrityFlag.created_at.desc()).all()

    return {
        "walker": walker_quality_item(profile, db),
        "score_breakdown": score_breakdown_payload(scores),
        "snapshots": [snapshot_payload(snapshot) for snapshot in snapshots],
        "reviews": [admin_review_payload(review) for review in reviews],
        "alerts": [alert_payload(alert) for alert in alerts],
        "recovery_plan": recovery_payload(recovery_plan) if recovery_plan else None,
        "incentives": [incentive_payload(incentive) for incentive in incentives],
        "tip_integrity_flags": [tip_flag_payload(flag) for flag in tip_flags],
        "recommendations": build_recommendations(walker_id, db),
        "tip_policy": TIP_REPUTATION_POLICY,
    }
