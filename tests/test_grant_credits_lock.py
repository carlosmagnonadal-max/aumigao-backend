"""FIX 2 (P1) — grant_credits_on_payment sem lock permitia conceder crédito 2x
sob duas entregas concorrentes de PAYMENT_CONFIRMED.

O fix adiciona releitura com with_for_update (igual reset_credits_if_renewal),
serializando as entregas: a 2ª vê credits_granted=True e desiste. SQLite não faz
locking real de linha, então validamos a IDEMPOTÊNCIA observável: duas concessões
seguidas concedem os créditos UMA vez só (walks_per_cycle, não 2x).
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # registra todas as tabelas
from app.core.database import Base
from app.models.tenant import Tenant
from app.models.user import User
from app.models.recurring_plan import RecurringPlan, TutorSubscription, SUBSCRIPTION_ACTIVE
from app.services.recurring_plan_service import grant_credits_on_payment
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-grant"
TUTOR_ID = "tutor-grant"


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="t@g.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.commit()
    return db, sessionmaker(bind=eng)


def _sub(db):
    now = datetime.utcnow()
    plan = RecurringPlan(tenant_id=TENANT_ID, name="P", price=80.0, walks_per_cycle=4, interval="monthly", active=True)
    db.add(plan); db.commit(); db.refresh(plan)
    sub = TutorSubscription(
        tenant_id=TENANT_ID, tutor_id=TUTOR_ID, plan_id=plan.id, status=SUBSCRIPTION_ACTIVE,
        walks_per_cycle=4, credits_remaining=0, credits_granted=False,
        current_period_start=now, current_period_end=now + timedelta(days=30),
    )
    db.add(sub); db.commit(); db.refresh(sub)
    return sub


def test_grant_credits_is_idempotent_no_double_grant():
    db, _ = _db()
    sub = _sub(db)

    assert grant_credits_on_payment(db, sub) is True
    db.commit()
    assert grant_credits_on_payment(db, sub) is False  # 2ª entrega: no-op
    db.commit()
    db.refresh(sub)

    # Créditos concedidos UMA vez só (4, não 8).
    assert sub.credits_remaining == 4
    assert sub.credits_granted is True


def test_grant_credits_uses_for_update_lock():
    # Garante que o caminho de concessão passa pela releitura com with_for_update
    # (a defesa contra a race). Sem o lock, o serviço agia direto no objeto passado.
    import app.services.recurring_plan_service as mod

    db, _ = _db()
    sub = _sub(db)

    calls = {"n": 0}
    real_query = db.query

    def spy_query(*a, **k):
        q = real_query(*a, **k)
        orig_ffu = q.with_for_update

        def wrapped(*aa, **kk):
            calls["n"] += 1
            return orig_ffu(*aa, **kk)

        q.with_for_update = wrapped
        return q

    db.query = spy_query
    assert grant_credits_on_payment(db, sub) is True
    assert calls["n"] >= 1  # o lock foi solicitado
    db.commit()
    db.refresh(sub)
    assert sub.credits_remaining == 4
