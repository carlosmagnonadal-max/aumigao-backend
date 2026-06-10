from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.incentive_rule import (
    INCENTIVES_FEATURE_KEY,
    REWARD_MONETARY,
    REWARD_VISIBILITY,
    TRIGGER_COMPLETED_MISSIONS,
    TRIGGER_COMPLETED_WALKS,
    TRIGGER_HYBRID_SCORE,
    TRIGGER_RATING,
    IncentiveRule,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walker_incentive import WalkerIncentive
from app.models.walker_profile import WalkerProfile
from app.services.reputation_service import (
    calculate_hybrid_reputation_score,
    reputation_summary,
)
from app.services.tenant_plan_service import tenant_has_feature
from app.services.weekly_mission_service import get_walker_mission_summary

# Mapeia reward_type -> incentive_type (categoria registrada no WalkerIncentive).
REWARD_TYPE_TO_INCENTIVE_TYPE = {
    "recognition": "recognition",
    "visibility": "visibility_boost",
    "monetary": "monetary",
}

# Regras default semeadas por tenant ao ligar a flag (1a avaliacao com flag on e
# sem regras). Monetario NAO tem default — o tenant cria/configura.
DEFAULT_INCENTIVE_RULES = [
    {
        "key": "well_rated",
        "title": "Passeador bem avaliado",
        "description": "Reconhecimento por manter uma avaliacao alta com volume minimo de avaliacoes.",
        "trigger_type": TRIGGER_RATING,
        "threshold": 4.8,
        "reward_type": "recognition",
        "reward_value": 0.0,
        "visibility_effect": "low",
        "active": True,
    },
    {
        "key": "consistent",
        "title": "Evolucao consistente",
        "description": "Voce concluiu missoes leves que fortalecem sua presenca profissional.",
        "trigger_type": TRIGGER_COMPLETED_MISSIONS,
        "threshold": 3.0,
        "reward_type": "recognition",
        "reward_value": 0.0,
        "visibility_effect": "low",
        "active": True,
    },
    {
        "key": "weekly_highlight",
        "title": "Destaque da semana",
        "description": "Elegibilidade para destaque controlado por qualidade real. Sem relacao com gorjetas.",
        "trigger_type": TRIGGER_HYBRID_SCORE,
        "threshold": 88.0,
        "reward_type": "visibility",
        "reward_value": 0.0,
        "visibility_effect": "medium",
        "active": True,
    },
]

# rating: mantem o piso de volume minimo de avaliacoes (5) da regra original.
RATING_MIN_REVIEWS = 5


def incentive_payload(incentive: WalkerIncentive) -> dict:
    return {
        "id": incentive.id,
        "walker_id": incentive.walker_id,
        "incentive_type": incentive.incentive_type,
        "title": incentive.title,
        "description": incentive.description,
        "source": incentive.source,
        "reward_type": getattr(incentive, "reward_type", "recognition"),
        "amount": getattr(incentive, "amount", 0.0) or 0.0,
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
    reward_type: str = "recognition",
    amount: float = 0.0,
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
        reward_type=reward_type,
        amount=amount or 0.0,
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


def resolve_walker_tenant(walker_id: str, db: Session) -> Tenant | None:
    """Resolve o tenant do passeador (via User.tenant_id; fallback tenant padrao)."""
    user = db.get(User, walker_id)
    if user and user.tenant_id:
        tenant = db.get(Tenant, user.tenant_id)
        if tenant:
            return tenant
    # Fallback para o tenant padrao (beta), evitando import no topo (ciclo).
    from app.services.tenant_context import get_default_tenant

    try:
        return get_default_tenant(db)
    except Exception:
        return None


def seed_default_incentive_rules(tenant_id: str, db: Session) -> list[IncentiveRule]:
    """Semeia as 3 regras default no tenant (idempotente por (tenant_id, key))."""
    created: list[IncentiveRule] = []
    for spec in DEFAULT_INCENTIVE_RULES:
        exists = (
            db.query(IncentiveRule)
            .filter(IncentiveRule.tenant_id == tenant_id, IncentiveRule.key == spec["key"])
            .first()
        )
        if exists:
            continue
        rule = IncentiveRule(id=str(uuid4()), tenant_id=tenant_id, **spec)
        db.add(rule)
        created.append(rule)
    if created:
        db.commit()
    return created


def _rule_threshold_met(rule: IncentiveRule, *, scores: dict, summary: dict, mission_summary: dict) -> bool:
    if rule.trigger_type == TRIGGER_RATING:
        return summary["reviews_count"] >= RATING_MIN_REVIEWS and summary["rating_average"] >= rule.threshold
    if rule.trigger_type == TRIGGER_COMPLETED_MISSIONS:
        return mission_summary.get("completed_missions", 0) >= rule.threshold
    if rule.trigger_type == TRIGGER_HYBRID_SCORE:
        return scores["hybrid_reputation_score"] >= rule.threshold and scores["risk_level"] == "normal"
    if rule.trigger_type == TRIGGER_COMPLETED_WALKS:
        return summary.get("total_walks", 0) >= rule.threshold
    return False


def evaluate_incentives(walker_id: str, db: Session) -> list[WalkerIncentive]:
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == walker_id).first()
    if not profile or profile.status != "approved":
        return get_active_incentives(walker_id, db)

    # Gating por flag de tenant: engine so concede se `incentives` estiver ligado.
    tenant = resolve_walker_tenant(walker_id, db)
    if not tenant or not tenant_has_feature(tenant, db, INCENTIVES_FEATURE_KEY):
        return get_active_incentives(walker_id, db)

    scores = calculate_hybrid_reputation_score(walker_id, db)
    if scores["risk_level"] in {"risk", "critical", "suspended"}:
        return get_active_incentives(walker_id, db)

    # Seed dos defaults quando a flag esta on e o tenant ainda nao tem regras.
    rules = db.query(IncentiveRule).filter(IncentiveRule.tenant_id == tenant.id).all()
    if not rules:
        seed_default_incentive_rules(tenant.id, db)
        rules = db.query(IncentiveRule).filter(IncentiveRule.tenant_id == tenant.id).all()

    summary = reputation_summary(walker_id, db)
    mission_summary = get_walker_mission_summary(walker_id, db)

    for rule in rules:
        if not rule.active:
            continue
        if not _rule_threshold_met(rule, scores=scores, summary=summary, mission_summary=mission_summary):
            continue
        incentive_type = REWARD_TYPE_TO_INCENTIVE_TYPE.get(rule.reward_type, "recognition")
        amount = rule.reward_value if rule.reward_type == REWARD_MONETARY else 0.0
        grant_incentive(
            walker_id,
            incentive_type,
            rule.title,
            rule.description,
            "incentive_rule",
            db,
            visibility_effect=rule.visibility_effect if rule.reward_type == REWARD_VISIBILITY else "none",
            reward_type=rule.reward_type,
            amount=amount,
        )

    return get_active_incentives(walker_id, db)


def list_incentives(walker_id: str, db: Session) -> list[WalkerIncentive]:
    expire_incentives(walker_id, db)
    return db.query(WalkerIncentive).filter(WalkerIncentive.walker_id == walker_id).order_by(WalkerIncentive.created_at.desc()).all()
