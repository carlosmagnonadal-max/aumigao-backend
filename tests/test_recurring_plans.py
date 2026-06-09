import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.recurring_plan import (
    SUBSCRIPTION_ACTIVE,
    SUBSCRIPTION_CANCELLED,
    RecurringPlan,
    TutorSubscription,
)
from app.models.tenant import Tenant, TenantFeature
from app.services import recurring_plan_service as svc


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            TenantFeature.__table__,
            RecurringPlan.__table__,
            TutorSubscription.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


def _tenant(db, *, with_feature: bool) -> Tenant:
    tenant = Tenant(id="t1", name="Aumigao", slug="aumigao", status="active", plan="business")
    db.add(tenant)
    if with_feature:
        db.add(TenantFeature(tenant_id=tenant.id, feature_key="recurring_plans", enabled=True))
    db.commit()
    return tenant


def _plan(db, tenant_id: str, *, price=99.0, walks=8, active=True) -> RecurringPlan:
    plan = RecurringPlan(tenant_id=tenant_id, name="Plano Mensal", price=price, walks_per_cycle=walks, active=active)
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def test_subscribe_blocked_when_feature_disabled():
    db = _db()
    tenant = _tenant(db, with_feature=False)
    plan = _plan(db, tenant.id)
    with pytest.raises(HTTPException) as exc:
        svc.subscribe(db, tenant, "tutor1", plan.id)
    assert exc.value.status_code == 403


def test_recurring_plans_enabled_reflects_feature_flag():
    db = _db()
    tenant = _tenant(db, with_feature=True)
    assert svc.recurring_plans_enabled(tenant, db) is True


def test_subscribe_grants_credits_and_period():
    db = _db()
    tenant = _tenant(db, with_feature=True)
    plan = _plan(db, tenant.id, walks=8)

    sub = svc.subscribe(db, tenant, "tutor1", plan.id)

    assert sub.status == SUBSCRIPTION_ACTIVE
    assert sub.credits_remaining == 8
    assert sub.walks_per_cycle == 8
    assert sub.price == plan.price
    assert sub.current_period_end is not None
    assert sub.current_period_end > sub.current_period_start


def test_subscribe_keeps_single_active_subscription():
    db = _db()
    tenant = _tenant(db, with_feature=True)
    plan_a = _plan(db, tenant.id, price=99.0)
    plan_b = _plan(db, tenant.id, price=149.0)

    first = svc.subscribe(db, tenant, "tutor1", plan_a.id)
    second = svc.subscribe(db, tenant, "tutor1", plan_b.id)

    db.refresh(first)
    assert first.status == SUBSCRIPTION_CANCELLED
    assert second.status == SUBSCRIPTION_ACTIVE
    active = svc.get_active_subscription(db, tenant.id, "tutor1")
    assert active.id == second.id


def test_subscribe_to_inactive_plan_rejected():
    db = _db()
    tenant = _tenant(db, with_feature=True)
    plan = _plan(db, tenant.id, active=False)
    with pytest.raises(HTTPException) as exc:
        svc.subscribe(db, tenant, "tutor1", plan.id)
    assert exc.value.status_code == 409


def test_cancel_subscription():
    db = _db()
    tenant = _tenant(db, with_feature=True)
    plan = _plan(db, tenant.id)
    svc.subscribe(db, tenant, "tutor1", plan.id)

    cancelled = svc.cancel_subscription(db, tenant.id, "tutor1")
    assert cancelled.status == SUBSCRIPTION_CANCELLED
    assert cancelled.cancelled_at is not None
    assert svc.get_active_subscription(db, tenant.id, "tutor1") is None


def test_cancel_without_active_subscription_raises():
    db = _db()
    tenant = _tenant(db, with_feature=True)
    with pytest.raises(HTTPException) as exc:
        svc.cancel_subscription(db, tenant.id, "tutor1")
    assert exc.value.status_code == 404


def test_list_plans_only_active_filter():
    db = _db()
    tenant = _tenant(db, with_feature=True)
    _plan(db, tenant.id, price=99.0, active=True)
    _plan(db, tenant.id, price=149.0, active=False)

    assert len(svc.list_plans(db, tenant.id, only_active=True)) == 1
    assert len(svc.list_plans(db, tenant.id, only_active=False)) == 2
