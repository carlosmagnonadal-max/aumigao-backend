"""Servico admin de Incentivos (Incentivos — spec 2026-06-10).

CRUD das IncentiveRule por tenant + concessao manual + revoke + listagem das
concessoes (WalkerIncentive). Monetario apenas REGISTRA amount; payout/split e
follow-up (NAO tocar pagamento aqui).
"""
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.incentive_rule import (
    REWARD_TYPES,
    TRIGGER_TYPES,
    IncentiveRule,
)
from app.models.user import User
from app.models.walker_incentive import WalkerIncentive
from app.services.incentive_engine_service import (
    REWARD_TYPE_TO_INCENTIVE_TYPE,
    grant_incentive,
    incentive_payload,
    revoke_incentive,
)


# --------------------------------------------------------------------------- #
# IncentiveRule CRUD (scoped por tenant)
# --------------------------------------------------------------------------- #
def _validate_types(trigger_type: str | None, reward_type: str | None) -> None:
    if trigger_type is not None and trigger_type not in TRIGGER_TYPES:
        raise HTTPException(status_code=422, detail=f"trigger_type invalido: {trigger_type}")
    if reward_type is not None and reward_type not in REWARD_TYPES:
        raise HTTPException(status_code=422, detail=f"reward_type invalido: {reward_type}")


def list_rules(tenant_id: str, db: Session) -> list[IncentiveRule]:
    return (
        db.query(IncentiveRule)
        .filter(IncentiveRule.tenant_id == tenant_id)
        .order_by(IncentiveRule.created_at.asc())
        .all()
    )


def get_rule_by_key(tenant_id: str, key: str, db: Session) -> IncentiveRule | None:
    return (
        db.query(IncentiveRule)
        .filter(IncentiveRule.tenant_id == tenant_id, IncentiveRule.key == key)
        .first()
    )


def get_rule_or_404(tenant_id: str, rule_id: str, db: Session) -> IncentiveRule:
    rule = (
        db.query(IncentiveRule)
        .filter(IncentiveRule.tenant_id == tenant_id, IncentiveRule.id == rule_id)
        .first()
    )
    if not rule:
        raise HTTPException(status_code=404, detail="Regra de incentivo nao encontrada")
    return rule


def create_rule(tenant_id: str, data: dict, db: Session) -> IncentiveRule:
    key = (data.get("key") or "").strip()
    if not key:
        raise HTTPException(status_code=422, detail="key obrigatoria")
    _validate_types(data.get("trigger_type"), data.get("reward_type"))
    if get_rule_by_key(tenant_id, key, db):
        raise HTTPException(status_code=409, detail="Ja existe uma regra com essa key neste tenant.")
    rule = IncentiveRule(id=str(uuid4()), tenant_id=tenant_id, **{**data, "key": key})
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


def update_rule(tenant_id: str, rule_id: str, values: dict, db: Session) -> IncentiveRule:
    rule = get_rule_or_404(tenant_id, rule_id, db)
    _validate_types(values.get("trigger_type"), values.get("reward_type"))
    for field, value in values.items():
        setattr(rule, field, value)
    db.commit()
    db.refresh(rule)
    return rule


# --------------------------------------------------------------------------- #
# Concessoes (WalkerIncentive)
# --------------------------------------------------------------------------- #
def grant_manual(tenant_id: str, walker_id: str, data: dict, db: Session) -> dict:
    """Concede um incentivo manual ao passeador (valida que pertence ao tenant)."""
    _ensure_walker_in_tenant(tenant_id, walker_id, db)
    reward_type = data.get("reward_type", "recognition")
    incentive_type = data.get("incentive_type") or REWARD_TYPE_TO_INCENTIVE_TYPE.get(reward_type, "recognition")
    incentive = grant_incentive(
        walker_id,
        incentive_type,
        data["title"],
        data.get("description") or "",
        data.get("source", "admin"),
        db,
        visibility_effect=data.get("visibility_effect", "none"),
        expires_at=data.get("expires_at"),
        admin_notes=data.get("admin_notes"),
        reward_type=reward_type,
        amount=data.get("amount", 0.0) or 0.0,
    )
    return incentive_payload(incentive)


def revoke_granted(tenant_id: str, incentive_id: str, db: Session, admin_notes: str | None = None) -> dict:
    incentive = db.get(WalkerIncentive, incentive_id)
    if not incentive:
        raise HTTPException(status_code=404, detail="Incentivo nao encontrado")
    _ensure_walker_in_tenant(tenant_id, incentive.walker_id, db)
    revoked = revoke_incentive(incentive_id, db, admin_notes=admin_notes)
    return incentive_payload(revoked)


def list_granted(tenant_id: str, db: Session, walker_id: str | None = None, status: str | None = None) -> list[dict]:
    """Lista incentivos concedidos aos passeadores DESTE tenant."""
    walker_ids = [u.id for u in db.query(User.id).filter(User.tenant_id == tenant_id).all()]
    if not walker_ids:
        return []
    query = db.query(WalkerIncentive).filter(WalkerIncentive.walker_id.in_(walker_ids))
    if walker_id:
        query = query.filter(WalkerIncentive.walker_id == walker_id)
    if status and status != "all":
        query = query.filter(WalkerIncentive.status == status)
    rows = query.order_by(WalkerIncentive.created_at.desc()).all()
    return [incentive_payload(row) for row in rows]


def _ensure_walker_in_tenant(tenant_id: str, walker_id: str, db: Session) -> None:
    user = db.get(User, walker_id)
    if not user:
        raise HTTPException(status_code=404, detail="Passeador nao encontrado")
    # tenant_id None (legado/beta) e tratado como pertencente ao tenant atual.
    if user.tenant_id and user.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Passeador nao encontrado")
