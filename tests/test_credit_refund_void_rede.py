"""P2 — void-de-rede automático no estorno da COMPRA DE CRÉDITO (refund sem walk_id).

Cobre:
  1. Reversão total: créditos NÃO consumidos → zerados + reversão de passivo no ledger.
  2. Idempotência: refund duplicado não reverte/zera 2×.
  3. Parcialmente consumido: zera o remanescente (seguro) + ALERTA admin (ambíguo),
     sem clawback automático do ganho do passeador.
  4. Refund de Payment sem relação com crédito (avulso/saque) → comportamento atual,
     nada de reversão de crédito.
"""
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base
from app.core.database import Base, get_global_db
from app.models.credit_ledger import CreditLedgerEntry, LEDGER_LIABILITY_REVERSED
from app.models.notification import Notification
from app.models.payment import Payment
from app.models.pet import Pet
from app.models.recurring_plan import (
    RecurringPlan, TutorSubscription, SUBSCRIPTION_ACTIVE,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

_TOKEN = "segredo-credit-refund"
_HEADERS = {"asaas-access-token": _TOKEN}
TENANT_ID = "t-cr"
TUTOR_ID = "tutor-cr"


def _client_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="t@cr.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(User(id="admin-cr", email="a@cr.com", password_hash="x", role="admin", tenant_id=TENANT_ID))
    db.add(Pet(id="pet-cr", tutor_id=TUTOR_ID, tenant_id=TENANT_ID, name="Rex"))
    db.commit()

    from app.routes import payments as pr
    test_app = FastAPI()
    test_app.include_router(pr.router)
    test_app.dependency_overrides[get_global_db] = lambda: db
    return TestClient(test_app), db


def _seed_subscription(db, *, credits_remaining=4, walks_per_cycle=4, price=80.0, asaas_sub="asaas-cr-1"):
    plan = RecurringPlan(
        tenant_id=TENANT_ID, name="Plano", price=price,
        walks_per_cycle=walks_per_cycle, interval="monthly", active=True,
    )
    db.add(plan); db.commit(); db.refresh(plan)
    now = datetime.utcnow()
    sub = TutorSubscription(
        id="sub-cr", tenant_id=TENANT_ID, plan_id=plan.id, tutor_id=TUTOR_ID,
        status=SUBSCRIPTION_ACTIVE, price=price, walks_per_cycle=walks_per_cycle,
        credits_remaining=credits_remaining, credits_granted=True,
        current_period_start=now, current_period_end=now + timedelta(days=30),
        asaas_subscription_id=asaas_sub,
    )
    db.add(sub); db.commit()
    return sub


def _refund_payload(sub, pid="pay-cr-1", event="PAYMENT_REFUNDED"):
    return {
        "event": event,
        "payment": {
            "id": pid, "status": "REFUNDED",
            "externalReference": f"sub:{sub.id}", "subscription": sub.asaas_subscription_id,
        },
    }


def _post(client, payload):
    return client.post("/payments/webhooks/asaas", json=payload, headers=_HEADERS)


# ---------------------------------------------------------------------------
# 1. Reversão total — créditos não consumidos
# ---------------------------------------------------------------------------
def test_refund_reverses_unused_credits_and_ledger(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", _TOKEN)
    client, db = _client_db()
    sub = _seed_subscription(db, credits_remaining=4)

    r = _post(client, _refund_payload(sub))
    assert r.status_code == 200, r.text

    db.expire_all()
    sub2 = db.get(TutorSubscription, sub.id)
    assert sub2.credits_remaining == 0, "créditos não usados devem ser zerados"

    ledger = db.query(CreditLedgerEntry).filter_by(
        subscription_id=sub.id, event_type=LEDGER_LIABILITY_REVERSED
    ).all()
    assert len(ledger) == 1
    assert ledger[0].credits_count == 4
    assert ledger[0].total_value < 0, "reversão de passivo deve ser negativa"
    # Sem passeios consumidos → sem alerta.
    assert db.query(Notification).filter_by(type="credit_refund_review").count() == 0


# ---------------------------------------------------------------------------
# 2. Idempotência — refund duplicado
# ---------------------------------------------------------------------------
def test_refund_is_idempotent(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", _TOKEN)
    client, db = _client_db()
    sub = _seed_subscription(db, credits_remaining=4)

    # 1º refund (event-id distinto para não bater na dedup de webhook_events)
    p1 = _refund_payload(sub, pid="pay-cr-1"); p1["id"] = "evt-1"
    assert _post(client, p1).status_code == 200
    # 2º refund (mesmo estorno, event-id diferente — testa idempotência da lógica de crédito)
    p2 = _refund_payload(sub, pid="pay-cr-1"); p2["id"] = "evt-2"
    assert _post(client, p2).status_code == 200

    db.expire_all()
    # Só UMA reversão de passivo, não duas.
    ledger = db.query(CreditLedgerEntry).filter_by(
        subscription_id=sub.id, event_type=LEDGER_LIABILITY_REVERSED
    ).all()
    assert len(ledger) == 1
    assert db.get(TutorSubscription, sub.id).credits_remaining == 0


# ---------------------------------------------------------------------------
# 3. Parcialmente consumido — reversão parcial segura + alerta (sem clawback)
# ---------------------------------------------------------------------------
def test_refund_partial_consumption_alerts_and_reverses_remaining(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", _TOKEN)
    client, db = _client_db()
    # 4 comprados, 1 consumido num passeio de rede → 3 remanescentes
    sub = _seed_subscription(db, credits_remaining=3, walks_per_cycle=4)
    db.add(Walk(
        id="walk-net-1", tutor_id=TUTOR_ID, tenant_id=TENANT_ID, pet_id="pet-cr",
        scheduled_date="2026-07-01", duration_minutes=30, price=50.0,
        status="Agendado", subscription_id=sub.id, credit_refunded=False,
        created_at=datetime.utcnow(),
    ))
    db.commit()

    r = _post(client, _refund_payload(sub))
    assert r.status_code == 200, r.text

    db.expire_all()
    # Remanescentes zerados (seguro).
    assert db.get(TutorSubscription, sub.id).credits_remaining == 0
    # Reversão de passivo pelos 3 remanescentes.
    ledger = db.query(CreditLedgerEntry).filter_by(
        subscription_id=sub.id, event_type=LEDGER_LIABILITY_REVERSED
    ).one()
    assert ledger.credits_count == 3
    # Alerta emitido para o admin (caso ambíguo — passeio já consumido).
    alerts = db.query(Notification).filter_by(type="credit_refund_review").all()
    assert len(alerts) == 1
    assert alerts[0].user_id == "admin-cr"


# ---------------------------------------------------------------------------
# 4. Refund sem relação com crédito — nada de reversão de crédito
# ---------------------------------------------------------------------------
def test_refund_unrelated_payment_no_credit_reversal(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", _TOKEN)
    client, db = _client_db()
    sub = _seed_subscription(db, credits_remaining=4)
    # Payment avulso sem externalReference sub: e sem subscription — não é compra de crédito.
    db.add(Payment(
        id="p-avulso", tenant_id=TENANT_ID, tutor_id=TUTOR_ID, walk_id=None,
        amount=50, status="pagamento_confirmado_sandbox", provider="asaas_sandbox",
        provider_payment_id="pay-avulso",
    ))
    db.commit()

    r = _post(client, {"event": "PAYMENT_REFUNDED", "payment": {"id": "pay-avulso"}})
    assert r.status_code in (200, 204), r.text

    db.expire_all()
    # Assinatura intacta, nenhum ledger de reversão, nenhum alerta.
    assert db.get(TutorSubscription, sub.id).credits_remaining == 4
    assert db.query(CreditLedgerEntry).filter_by(event_type=LEDGER_LIABILITY_REVERSED).count() == 0
    assert db.query(Notification).filter_by(type="credit_refund_review").count() == 0
