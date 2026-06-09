"""Regras de negócio dos planos recorrentes (Onda 1).

Catálogo por tenant + ciclo de vida da assinatura do tutor + concessão de
créditos por ciclo. A cobrança recorrente real no gateway é Sprint 16 (Fase B).
"""
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.recurring_plan import (
    RECURRING_PLANS_FEATURE_KEY,
    SUBSCRIPTION_ACTIVE,
    SUBSCRIPTION_CANCELLED,
    RecurringPlan,
    TutorSubscription,
)
from app.models.tenant import Tenant
from app.services.tenant_plan_service import enforce_tenant_product_feature, tenant_has_feature

CYCLE_DAYS = 30
FEATURE_LABEL = "Planos recorrentes"


def recurring_plans_enabled(tenant: Tenant, db: Session) -> bool:
    return tenant_has_feature(tenant, db, RECURRING_PLANS_FEATURE_KEY)


def enforce_enabled(tenant: Tenant, db: Session) -> None:
    enforce_tenant_product_feature(tenant, db, RECURRING_PLANS_FEATURE_KEY, FEATURE_LABEL)


def list_plans(db: Session, tenant_id: str, *, only_active: bool) -> list[RecurringPlan]:
    query = db.query(RecurringPlan).filter(RecurringPlan.tenant_id == tenant_id)
    if only_active:
        query = query.filter(RecurringPlan.active.is_(True))
    return query.order_by(RecurringPlan.price.asc()).all()


def get_plan_or_404(db: Session, tenant_id: str, plan_id: str) -> RecurringPlan:
    plan = (
        db.query(RecurringPlan)
        .filter(RecurringPlan.tenant_id == tenant_id, RecurringPlan.id == plan_id)
        .first()
    )
    if not plan:
        raise HTTPException(status_code=404, detail="Plano recorrente não encontrado.")
    return plan


def get_active_subscription(db: Session, tenant_id: str, tutor_id: str) -> TutorSubscription | None:
    return (
        db.query(TutorSubscription)
        .filter(
            TutorSubscription.tenant_id == tenant_id,
            TutorSubscription.tutor_id == tutor_id,
            TutorSubscription.status == SUBSCRIPTION_ACTIVE,
        )
        .order_by(TutorSubscription.created_at.desc())
        .first()
    )


def subscribe(db: Session, tenant: Tenant, tutor_id: str, plan_id: str) -> TutorSubscription:
    enforce_enabled(tenant, db)
    plan = get_plan_or_404(db, tenant.id, plan_id)
    if not plan.active:
        raise HTTPException(status_code=409, detail="Este plano não está disponível para assinatura.")

    now = datetime.utcnow()
    # Mantém uma assinatura ativa por tutor: cancela a anterior (troca de plano).
    existing = get_active_subscription(db, tenant.id, tutor_id)
    if existing:
        existing.status = SUBSCRIPTION_CANCELLED
        existing.cancelled_at = now
        existing.updated_at = now
        db.add(existing)

    subscription = TutorSubscription(
        tenant_id=tenant.id,
        plan_id=plan.id,
        tutor_id=tutor_id,
        status=SUBSCRIPTION_ACTIVE,
        price=plan.price,
        walks_per_cycle=plan.walks_per_cycle,
        credits_remaining=plan.walks_per_cycle,
        current_period_start=now,
        current_period_end=now + timedelta(days=CYCLE_DAYS),
    )
    db.add(subscription)
    db.commit()
    db.refresh(subscription)
    return subscription


def cancel_subscription(db: Session, tenant_id: str, tutor_id: str) -> TutorSubscription:
    subscription = get_active_subscription(db, tenant_id, tutor_id)
    if not subscription:
        raise HTTPException(status_code=404, detail="Nenhuma assinatura ativa para cancelar.")
    now = datetime.utcnow()
    subscription.status = SUBSCRIPTION_CANCELLED
    subscription.cancelled_at = now
    subscription.updated_at = now
    db.add(subscription)
    db.commit()
    db.refresh(subscription)
    return subscription


def plan_name_for(db: Session, subscription: TutorSubscription | None) -> str | None:
    if not subscription:
        return None
    plan = db.get(RecurringPlan, subscription.plan_id)
    return plan.name if plan else None
