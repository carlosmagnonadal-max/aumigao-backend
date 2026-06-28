from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.tip_integrity_flag import TipIntegrityFlag
from app.models.user import User
from app.models.walk import Walk
from app.models.walker_incentive import WalkerIncentive
from app.models.walker_monitoring_alert import WalkerMonitoringAlert
from app.models.walker_profile import WalkerProfile
from app.models.walker_recovery_plan import WalkerRecoveryPlan
from app.models.walker_reputation_snapshot import WalkerReputationSnapshot
from app.models.walker_review import WalkerReview
from app.services.incentive_engine_service import evaluate_incentives, incentive_payload, list_incentives
from app.services.monitoring_service import alert_payload, evaluate_monitoring_alerts, open_alerts
from app.services.recovery_service import active_recovery_plan, build_recommendations, get_or_create_recovery_plan, recovery_payload
from app.services.reputation_service import (
    COMPLETED_STATUSES,
    admin_review_payload,
    calculate_basic_behavior_score,
    calculate_hybrid_reputation_score,
    create_reputation_snapshot,
    get_walker_identity,
    reputation_summary,
    walker_level,
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
        return "Ha sugestoes disponiveis caso queira melhorar sua experiencia nos proximos passeios."
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


def _safe_count_map(query, key_index: int = 0, value_index: int = 1) -> dict:
    try:
        return {row[key_index]: row[value_index] for row in query.all()}
    except Exception:
        try:
            query.session.rollback()
        except Exception:
            pass
        return {}


def _safe_review_map(query) -> dict:
    try:
        return {row[0]: (row[1], row[2]) for row in query.all()}
    except Exception:
        try:
            query.session.rollback()
        except Exception:
            pass
        return {}


def _safe_latest_snapshots(db: Session, walker_ids: list[str]) -> dict:
    if not walker_ids:
        return {}
    try:
        snapshots = (
            db.query(WalkerReputationSnapshot)
            .filter(WalkerReputationSnapshot.walker_id.in_(walker_ids))
            .order_by(WalkerReputationSnapshot.calculated_at.desc())
            .limit(len(walker_ids) * 5)
            .all()
        )
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return {}
    latest = {}
    for snapshot in snapshots:
        latest.setdefault(snapshot.walker_id, snapshot)
    return latest


def _light_walker_quality_items(profiles: list[WalkerProfile], db: Session) -> list[dict]:
    # Otimizacao beta: lista administrativa usa agregados leves e evita recalcular reputacao de todos.
    walker_ids = [profile.user_id for profile in profiles if profile.user_id]
    if not walker_ids:
        return []

    review_rows = _safe_review_map(
        db.query(
            WalkerReview.walker_id,
            func.count(WalkerReview.id),
            func.coalesce(func.avg(WalkerReview.rating), 0),
        )
        .filter(WalkerReview.walker_id.in_(walker_ids))
        .group_by(WalkerReview.walker_id)
    )
    completed_counts = _safe_count_map(
        db.query(Walk.walker_id, func.count(Walk.id))
        .filter(Walk.walker_id.in_(walker_ids), Walk.status.in_(COMPLETED_STATUSES))
        .group_by(Walk.walker_id)
    )
    total_walk_counts = _safe_count_map(
        db.query(Walk.walker_id, func.count(Walk.id))
        .filter(Walk.walker_id.in_(walker_ids))
        .group_by(Walk.walker_id)
    )
    cancelled_counts = _safe_count_map(
        db.query(Walk.walker_id, func.count(Walk.id))
        .filter(Walk.walker_id.in_(walker_ids), func.lower(Walk.status) == "cancelado")
        .group_by(Walk.walker_id)
    )
    alert_counts = _safe_count_map(
        db.query(WalkerMonitoringAlert.walker_id, func.count(WalkerMonitoringAlert.id))
        .filter(WalkerMonitoringAlert.walker_id.in_(walker_ids), WalkerMonitoringAlert.status == "open")
        .group_by(WalkerMonitoringAlert.walker_id)
    )
    incentive_counts = _safe_count_map(
        db.query(WalkerIncentive.walker_id, func.count(WalkerIncentive.id))
        .filter(WalkerIncentive.walker_id.in_(walker_ids), WalkerIncentive.status.in_(["active", "granted"]))
        .group_by(WalkerIncentive.walker_id)
    )
    recovery_counts = _safe_count_map(
        db.query(WalkerRecoveryPlan.walker_id, func.count(WalkerRecoveryPlan.id))
        .filter(WalkerRecoveryPlan.walker_id.in_(walker_ids), WalkerRecoveryPlan.status == "active")
        .group_by(WalkerRecoveryPlan.walker_id)
    )
    tip_counts = _safe_count_map(
        db.query(TipIntegrityFlag.walker_id, func.count(TipIntegrityFlag.id))
        .filter(TipIntegrityFlag.walker_id.in_(walker_ids), TipIntegrityFlag.status == "open")
        .group_by(TipIntegrityFlag.walker_id)
    )
    latest_snapshots = _safe_latest_snapshots(db, walker_ids)

    rows = []
    for profile in profiles:
        walker_id = profile.user_id
        if not walker_id:
            continue
        reviews_count, rating_average = review_rows.get(walker_id, (0, 0))
        total_walks = int(completed_counts.get(walker_id, 0) or 0)
        all_walks = int(total_walk_counts.get(walker_id, 0) or 0)
        cancellations = int(cancelled_counts.get(walker_id, 0) or 0)
        snapshot = latest_snapshots.get(walker_id)
        hybrid_score = float(snapshot.hybrid_reputation_score) if snapshot else 75.0
        risk_level = snapshot.risk_level if snapshot else "normal"
        rows.append({
            "walker_id": walker_id,
            "name": profile.full_name or "Passeador Aumigao",
            "status": profile.status,
            "rating_average": round(float(rating_average or 0), 2),
            "reviews_count": int(reviews_count or 0),
            "total_walks": total_walks,
            "level": walker_level(total_walks, float(rating_average or 0), int(reviews_count or 0)),
            "hybrid_reputation_score": hybrid_score,
            "risk_level": risk_level,
            "open_alerts_count": int(alert_counts.get(walker_id, 0) or 0),
            "active_incentives_count": int(incentive_counts.get(walker_id, 0) or 0),
            "active_recovery_plan": bool(recovery_counts.get(walker_id, 0)),
            "tip_flags_count": int(tip_counts.get(walker_id, 0) or 0),
            "cancellation_rate": round((cancellations / max(1, all_walks)) * 100, 2) if all_walks else 0.0,
        })
    return rows


def get_quality_dashboard(
    db: Session,
    risk_level: str | None = None,
    status: str | None = None,
    has_open_alerts: bool | None = None,
    has_recovery_plan: bool | None = None,
    has_tip_flags: bool | None = None,
    limit: int = 50,
) -> dict:
    query = db.query(WalkerProfile)
    if status and status != "all":
        query = query.filter(WalkerProfile.status == status)
    try:
        profiles = query.order_by(WalkerProfile.updated_at.desc(), WalkerProfile.created_at.desc()).limit(limit).all()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return {"items": [], "total": 0}

    rows = []
    for item in _light_walker_quality_items(profiles, db):
        if risk_level and risk_level != "all" and item["risk_level"] != risk_level:
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
