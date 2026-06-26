import os
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # registra todas as tabelas no Base
from app.core.database import Base, get_db, get_global_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.models.pet import Pet
from app.models.walk import Walk
from app.models.payment import Payment
from app.models.recurring_plan import (
    RecurringPlan, TutorSubscription, SUBSCRIPTION_ACTIVE,
)
from app.services.recurring_plan_service import (
    subscribe, get_active_subscription, consume_credit_if_available,
)
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-credits"
TUTOR_ID = "tutor-credits"


def _make_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(TenantFeature(tenant_id=TENANT_ID, feature_key="recurring_plans", enabled=True))
    db.add(User(id=TUTOR_ID, email="tutor@credits.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(Pet(id="pet-1", tutor_id=TUTOR_ID, tenant_id=TENANT_ID, name="Rex"))
    db.commit()
    return db


def _tenant(db):
    return db.get(Tenant, TENANT_ID)


def _make_plan(db, tenant, walks_per_cycle=4, price=80.0):
    plan = RecurringPlan(
        tenant_id=tenant.id, name="Plano Mensal", price=price,
        walks_per_cycle=walks_per_cycle, interval="monthly", active=True,
    )
    db.add(plan); db.commit(); db.refresh(plan)
    return plan


def _make_covered_walk(db, tenant, sub, created_at=None):
    walk = Walk(
        id=f"walk-{datetime.utcnow().timestamp()}",
        tutor_id=TUTOR_ID, tenant_id=tenant.id, pet_id="pet-1",
        scheduled_date="2026-07-01", duration_minutes=30, price=50.0,
        status="Agendado", subscription_id=sub.id, credit_refunded=False,
        created_at=created_at or datetime.utcnow(),
    )
    db.add(walk); db.commit()
    return walk


def test_consume_credit_decrements_when_available():
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    subscribe(db, tenant, TUTOR_ID, plan.id)

    sub = consume_credit_if_available(db, tenant, TUTOR_ID)
    db.commit()

    assert sub is not None
    assert sub.credits_remaining == 3


def test_consume_credit_none_without_subscription():
    db = _make_db(); tenant = _tenant(db)
    assert consume_credit_if_available(db, tenant, "tutor-sem-assinatura") is None


def test_consume_credit_none_when_no_credits():
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=1)
    subscribe(db, tenant, TUTOR_ID, plan.id)
    consume_credit_if_available(db, tenant, TUTOR_ID); db.commit()  # 1 -> 0
    assert consume_credit_if_available(db, tenant, TUTOR_ID) is None  # 0 -> None
