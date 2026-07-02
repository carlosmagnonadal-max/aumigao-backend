"""FIX 5 (P1) — tutor inadimplente (PAYMENT_OVERDUE) não consome crédito.

Antes, inadimplência do tutor não mudava o status da assinatura nem bloqueava o
consumo. Agora PAYMENT_OVERDUE do webhook `sub:` marca a assinatura como OVERDUE,
e consume_credit_if_available (que exige status ACTIVE) passa a devolver None.
PAYMENT_CONFIRMED posterior reativa (volta a ACTIVE).
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base
from app.models.tenant import Tenant
from app.models.user import User
from app.models.recurring_plan import (
    RecurringPlan, TutorSubscription,
    SUBSCRIPTION_ACTIVE, SUBSCRIPTION_OVERDUE,
)
from app.services.recurring_plan_service import consume_credit_if_available
from app.routes.payments import _handle_subscription_webhook
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-ov"
TUTOR_ID = "tutor-ov"


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="t@ov.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.commit()
    return db


def _sub(db, status=SUBSCRIPTION_ACTIVE, credits=4):
    now = datetime.utcnow()
    plan = RecurringPlan(tenant_id=TENANT_ID, name="P", price=80.0, walks_per_cycle=4, interval="monthly", active=True)
    db.add(plan); db.commit(); db.refresh(plan)
    sub = TutorSubscription(
        tenant_id=TENANT_ID, tutor_id=TUTOR_ID, plan_id=plan.id, status=status,
        walks_per_cycle=4, credits_remaining=credits, credits_granted=True,
        current_period_start=now, current_period_end=now + timedelta(days=30),
        asaas_subscription_id="asaas-sub-1",
    )
    db.add(sub); db.commit(); db.refresh(sub)
    return sub


def test_overdue_subscription_cannot_consume_credit():
    db = _db()
    sub = _sub(db, status=SUBSCRIPTION_OVERDUE, credits=4)
    tenant = db.get(Tenant, TENANT_ID)
    # Tutor inadimplente: consumo bloqueado apesar de ter créditos.
    assert consume_credit_if_available(db, tenant, TUTOR_ID) is None


def test_active_subscription_can_consume_credit():
    db = _db()
    _sub(db, status=SUBSCRIPTION_ACTIVE, credits=4)
    tenant = db.get(Tenant, TENANT_ID)
    result = consume_credit_if_available(db, tenant, TUTOR_ID)
    assert result is not None and result.credits_remaining == 3


def test_webhook_overdue_marks_subscription_overdue():
    db = _db()
    sub = _sub(db, status=SUBSCRIPTION_ACTIVE)
    payment_data = {
        "id": "pay-1",
        "externalReference": f"sub:{sub.id}",
        "subscription": "asaas-sub-1",
        "value": 80.0,
        "status": "OVERDUE",
    }
    _handle_subscription_webhook(db, "PAYMENT_OVERDUE", payment_data)
    db.refresh(sub)
    assert sub.status == SUBSCRIPTION_OVERDUE

    # E o consumo passa a ser bloqueado.
    tenant = db.get(Tenant, TENANT_ID)
    assert consume_credit_if_available(db, tenant, TUTOR_ID) is None


def test_webhook_confirmed_reactivates_overdue_subscription():
    db = _db()
    sub = _sub(db, status=SUBSCRIPTION_OVERDUE)
    payment_data = {
        "id": "pay-2",
        "externalReference": f"sub:{sub.id}",
        "subscription": "asaas-sub-1",
        "value": 80.0,
        "status": "CONFIRMED",
    }
    _handle_subscription_webhook(db, "PAYMENT_CONFIRMED", payment_data)
    db.refresh(sub)
    assert sub.status == SUBSCRIPTION_ACTIVE
