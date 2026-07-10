"""Fase 2: gasto do tutor (payments pagos) + avaliação de alertas owner_type=tutor."""
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base
from app.models.cost_alert import CostAlert, CostAlertEvent
from app.models.notification import Notification
from app.models.payment import Payment
from app.models.pet import Pet
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.services.cost_alert_service import evaluate_cost_alerts, tutor_spend

TENANT = "t-tutor-cost"
TUTOR = "tut-1"


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.info["rls_tenant"] = "*"
    db.add(Tenant(id=TENANT, name="T", slug="t-tutor-cost", status="active", plan="business"))
    db.add(User(id=TUTOR, email="tut@x.com", password_hash="x", role="cliente", tenant_id=TENANT))
    db.add(Pet(id="pet-a", tutor_id=TUTOR, tenant_id=TENANT, name="Aurora"))
    db.add(Pet(id="pet-b", tutor_id=TUTOR, tenant_id=TENANT, name="Bidu"))
    db.commit()
    return db


def _walk(db, walk_id, pet_id):
    db.add(Walk(id=walk_id, tutor_id=TUTOR, tenant_id=TENANT, pet_id=pet_id,
                scheduled_date="2026-07-15T10:00", duration_minutes=45, price=50.0,
                status="Agendado", operational_status="ride_scheduled"))
    db.commit()


def _payment(db, *, pay_id, amount, status="paid", walk_id=None, created_at=None):
    pay = Payment(id=pay_id, tenant_id=TENANT, tutor_id=TUTOR, amount=amount,
                  status=status, walk_id=walk_id, provider="asaas")
    db.add(pay)
    db.commit()
    if created_at is not None:
        db.query(Payment).filter(Payment.id == pay_id).update({"created_at": created_at})
        db.commit()


def _tutor_alert(db, *, budget=100.0, scope="total", thresholds="[80, 100]", evaluation="actual"):
    alert = CostAlert(id="ta-1", tenant_id=TENANT, owner_type="tutor", owner_user_id=TUTOR,
                      name="Meu orçamento", scope=scope, budget_amount=budget, period="monthly",
                      thresholds_json=thresholds, evaluation=evaluation, channels_json='["in_app"]')
    db.add(alert)
    db.commit()
    return alert


def test_tutor_spend_counts_only_paid_in_window():
    db = _db()
    now = datetime.utcnow()
    _payment(db, pay_id="p1", amount=30.0)                       # pago (default)
    _payment(db, pay_id="p2", amount=20.0, status="Pago")        # variante PT conta
    _payment(db, pay_id="p3", amount=99.0, status="pending")     # NÃO conta
    _payment(db, pay_id="p4", amount=77.0, created_at=now - timedelta(days=90))  # fora da janela
    start, end = now - timedelta(days=30), now + timedelta(days=1)
    assert tutor_spend(db, TUTOR, "total", start, end) == Decimal("50.00")


def test_tutor_spend_pet_scope_joins_walk():
    db = _db()
    now = datetime.utcnow()
    _walk(db, "w-a", "pet-a")
    _walk(db, "w-b", "pet-b")
    _payment(db, pay_id="p1", amount=30.0, walk_id="w-a")
    _payment(db, pay_id="p2", amount=20.0, walk_id="w-b")
    _payment(db, pay_id="p3", amount=10.0)  # sem walk (ex.: assinatura) — só no total
    start, end = now - timedelta(days=1), now + timedelta(days=1)
    assert tutor_spend(db, TUTOR, "pet:pet-a", start, end) == Decimal("30.00")
    assert tutor_spend(db, TUTOR, "total", start, end) == Decimal("60.00")


def test_evaluate_fires_tutor_alert_and_dedupes():
    db = _db()
    _tutor_alert(db, budget=100.0, thresholds="[80]")
    _payment(db, pay_id="p1", amount=85.0)
    assert evaluate_cost_alerts(db) == 1
    assert evaluate_cost_alerts(db) == 0  # índice único segura
    events = db.query(CostAlertEvent).all()
    assert len(events) == 1 and events[0].tenant_id == TENANT
    notif = db.query(Notification).filter(Notification.type == "cost_alert").all()
    assert len(notif) == 1
    assert notif[0].user_id == TUTOR  # notificação vai pro TUTOR, não pros admins


def test_tenant_alerts_still_work_alongside_tutor():
    """Regressão: a extensão não pode quebrar a avaliação de tenant (fase 1)."""
    from app.models.commission_entry import CommissionEntry
    db = _db()
    _tutor_alert(db, budget=100.0, thresholds="[80]")
    _payment(db, pay_id="p1", amount=85.0)
    db.add(CostAlert(id="tn-1", tenant_id=TENANT, owner_type="tenant", name="Tenant",
                     budget_amount=1.0, thresholds_json="[100]", evaluation="actual",
                     channels_json='["in_app"]'))
    db.add(CommissionEntry(id="ce1", tenant_id=TENANT, walk_id="w-x", period="2026-07",
                           walk_price=50.0, commission_percent=10.0, amount=5.0,
                           is_network=False, status="accrued"))
    db.commit()
    assert evaluate_cost_alerts(db) == 2  # 1 do tutor + 1 do tenant
