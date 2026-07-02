"""FIX 6 (P1) — dedup persistente de webhooks por event-id.

O mesmo evento do Asaas (mesmo `id` no topo do payload) enviado 2x não pode
reaplicar efeito financeiro. A tabela webhook_events grava o event-id com UNIQUE;
o 2º envio retorna 200 com duplicate=True sem tocar em dinheiro.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base, get_db, get_global_db
from app.models.payment import Payment
from app.models.tenant import Tenant
from app.models.user import User
from app.models.recurring_plan import RecurringPlan, TutorSubscription
from app.models.webhook_event import WebhookEvent
from app.routes import payments
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-dedup"
TUTOR_ID = "tutor-dedup"
TOKEN = "wh-secret-dedup"


def build():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="t@dedup.com", password_hash="x", role="tutor", tenant_id=TENANT_ID, is_active=True))
    db.add(RecurringPlan(id="plan-d", tenant_id=TENANT_ID, name="P", price=99.9, walks_per_cycle=4, interval="monthly", active=True))
    db.add(TutorSubscription(
        id="sub-d", tenant_id=TENANT_ID, plan_id="plan-d", tutor_id=TUTOR_ID, price=99.9,
        walks_per_cycle=4, credits_remaining=0, credits_granted=False, asaas_subscription_id="asaas-sub-d",
    ))
    db.commit()
    app = FastAPI()
    app.include_router(payments.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_global_db] = lambda: db
    return TestClient(app), db


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", TOKEN)
    monkeypatch.setattr(payments, "PAYMENT_MODE", "asaas_sandbox")


def _confirm_payload(event_id):
    return {
        "id": event_id,  # event-id no TOPO do payload (dedup key)
        "event": "PAYMENT_CONFIRMED",
        "payment": {
            "id": "asaas-pay-d",
            "externalReference": "sub:sub-d",
            "subscription": "asaas-sub-d",
            "value": 99.9,
            "status": "CONFIRMED",
        },
    }


def test_same_event_id_twice_grants_credits_once():
    client, db = build()
    h = {"asaas-access-token": TOKEN}

    r1 = client.post("/payments/webhooks/asaas", headers=h, json=_confirm_payload("evt_123"))
    assert r1.status_code == 200, r1.text
    assert r1.json().get("duplicate") is not True

    r2 = client.post("/payments/webhooks/asaas", headers=h, json=_confirm_payload("evt_123"))
    assert r2.status_code == 200, r2.text
    assert r2.json().get("duplicate") is True

    db.expire_all()
    sub = db.get(TutorSubscription, "sub-d")
    # Créditos concedidos UMA vez (4, não 8) — efeito financeiro não duplicado.
    assert sub.credits_remaining == 4
    # Só um WebhookEvent gravado.
    assert db.query(WebhookEvent).filter_by(event_id="evt_123").count() == 1


def test_different_event_ids_both_process():
    client, db = build()
    h = {"asaas-access-token": TOKEN}
    r1 = client.post("/payments/webhooks/asaas", headers=h, json=_confirm_payload("evt_a"))
    r2 = client.post("/payments/webhooks/asaas", headers=h, json=_confirm_payload("evt_b"))
    # Event-ids diferentes: nenhum é duplicata; ambos gravados.
    assert r1.json().get("duplicate") is not True
    assert r2.json().get("duplicate") is not True
    assert db.query(WebhookEvent).count() == 2


def test_failed_processing_drops_marker_so_retry_reprocesses(monkeypatch):
    # Se o handler falhar (500), o marcador de dedup é removido para o reenvio do
    # Asaas poder reprocessar. Simulamos falha no 1º envio e sucesso no 2º.
    client, db = build()
    h = {"asaas-access-token": TOKEN}

    calls = {"n": 0}
    real = payments._handle_subscription_webhook

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(*a, **k)

    monkeypatch.setattr(payments, "_handle_subscription_webhook", flaky)

    r1 = client.post("/payments/webhooks/asaas", headers=h, json=_confirm_payload("evt_retry"))
    assert r1.status_code == 500
    # Marcador removido -> não bloqueia o reenvio.
    assert db.query(WebhookEvent).filter_by(event_id="evt_retry").count() == 0

    r2 = client.post("/payments/webhooks/asaas", headers=h, json=_confirm_payload("evt_retry"))
    assert r2.status_code == 200, r2.text
    assert r2.json().get("duplicate") is not True
    db.expire_all()
    assert db.get(TutorSubscription, "sub-d").credits_remaining == 4


def test_payload_without_event_id_still_processes():
    # Sem `id` no topo: segue sem dedup (best-effort), não quebra.
    client, db = build()
    h = {"asaas-access-token": TOKEN}
    payload = _confirm_payload("ignored")
    del payload["id"]
    r = client.post("/payments/webhooks/asaas", headers=h, json=payload)
    assert r.status_code == 200, r.text
    assert db.query(WebhookEvent).count() == 0
