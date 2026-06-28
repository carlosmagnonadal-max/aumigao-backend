from datetime import datetime
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.walker_recovery_plan import WalkerRecoveryPlan
from app.services.reputation_service import calculate_basic_behavior_score, calculate_hybrid_reputation_score, reputation_summary


DEFAULT_RECOMMENDATIONS = [
    "Confirme sua agenda antes de aceitar novos passeios.",
    "Leia as instrucoes do tutor antes da retirada.",
    "Mantenha comunicacao clara durante o passeio.",
    "Chegue com alguns minutos de antecedencia quando possivel.",
    "Revise comentarios recentes para identificar pequenos ajustes.",
]


def recovery_payload(plan: WalkerRecoveryPlan) -> dict:
    return {
        "id": plan.id,
        "walker_id": plan.walker_id,
        "risk_level_at_start": plan.risk_level_at_start,
        "status": plan.status,
        "reason": plan.reason,
        "recommended_actions": plan.recommended_actions or [],
        "started_at": plan.started_at,
        "ends_at": plan.ends_at,
        "completed_at": plan.completed_at,
        "created_at": plan.created_at,
        "updated_at": plan.updated_at,
    }


def build_recommendations(walker_id: str, db: Session) -> list[str]:
    summary = reputation_summary(walker_id, db)
    behavior = calculate_basic_behavior_score(walker_id, db)
    recommendations = []

    if summary["reviews_count"] == 0:
        recommendations.append("Complete seus primeiros passeios para comecar a construir sua reputacao.")
    if summary["reviews_count"] >= 3 and summary["rating_average"] < 4.6:
        recommendations.append("Revise os comentarios recentes e escolha um ponto de melhoria por passeio.")
    if behavior["cancellation_rate"] >= 12:
        recommendations.append("Evite aceitar passeios quando houver risco de conflito de horario.")
    if not recommendations:
        recommendations.extend(DEFAULT_RECOMMENDATIONS[:3])
    return recommendations


def active_recovery_plan(walker_id: str, db: Session) -> WalkerRecoveryPlan | None:
    return (
        db.query(WalkerRecoveryPlan)
        .filter(WalkerRecoveryPlan.walker_id == walker_id, WalkerRecoveryPlan.status == "active")
        .order_by(WalkerRecoveryPlan.created_at.desc())
        .first()
    )


def get_or_create_recovery_plan(walker_id: str, db: Session, reason: str | None = None, actions: list[str] | None = None, force: bool = False) -> WalkerRecoveryPlan | None:
    scores = calculate_hybrid_reputation_score(walker_id, db)
    if not force and scores["risk_level"] not in {"attention", "risk", "critical"}:
        return active_recovery_plan(walker_id, db)

    existing = active_recovery_plan(walker_id, db)
    if existing:
        return existing

    plan = WalkerRecoveryPlan(
        id=str(uuid4()),
        walker_id=walker_id,
        risk_level_at_start=scores["risk_level"],
        reason=reason or "Reunimos algumas sugestoes opcionais para ajudar voce quando quiser.",
        recommended_actions=actions or build_recommendations(walker_id, db),
        started_at=datetime.utcnow(),
        ends_at=None,
        status="active",
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def update_recovery_plan_status(plan_id: str, status: str, db: Session) -> WalkerRecoveryPlan:
    plan = db.get(WalkerRecoveryPlan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plano de recuperacao nao encontrado")
    plan.status = status
    if status == "completed":
        plan.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(plan)
    return plan
