"""Testes de guards defensivos do webhook Asaas em POST /payments/webhooks/asaas.

Padrao do projeto: FastAPI minimo + SQLite StaticPool + override de get_db.
O token esperado e injetado via monkeypatch no env ASAAS_WEBHOOK_TOKEN.

Cobre:
- Token ausente → 401
- Token errado → 401
- Payload nao-dict (lista) → 400
- Payload sem campo 'event' → 400
- Campo 'event' vazio → 400
- Campo 'payment' nao-dict (numero) → 400

E) _handle_subscription_webhook via webhook:
- external_ref=sub:inexistente → 200 noop sem Payment
- PAYMENT_OVERDUE com assinatura valida → status=falha_pagamento no Payment local
"""
import os
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
from app.routes import payments
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-webhook"
TUTOR_ID = "tutor-webhook"
WEBHOOK_TOKEN = "test-webhook-secret-123"


def build():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="tutor@wh.com", password_hash="x",
                role="tutor", tenant_id=TENANT_ID, is_active=True))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(payments.router)
    test_app.dependency_overrides[get_db] = lambda: db
    # get_global_db e usado pelo webhook do Asaas; override para ver entidades em memoria.
    test_app.dependency_overrides[get_global_db] = lambda: db
    return TestClient(test_app), db


@pytest.fixture(autouse=True)
def _set_webhook_token(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", WEBHOOK_TOKEN)


@pytest.fixture(autouse=True)
def _force_sandbox(monkeypatch):
    monkeypatch.setattr(payments, "PAYMENT_MODE", "asaas_sandbox")


def _wh_headers():
    return {"asaas-access-token": WEBHOOK_TOKEN}


# ------------------------------------------------------------------- auth ---

def test_webhook_missing_token_returns_401():
    client, _ = build()
    r = client.post("/payments/webhooks/asaas",
                    json={"event": "PAYMENT_RECEIVED", "payment": {"id": "x"}})
    assert r.status_code == 401


def test_webhook_wrong_token_returns_401():
    client, _ = build()
    r = client.post("/payments/webhooks/asaas",
                    headers={"asaas-access-token": "token-errado"},
                    json={"event": "PAYMENT_RECEIVED"})
    assert r.status_code == 401


# ----------------------------------------------- guards estruturais (C) ---

def test_webhook_payload_not_dict_returns_4xx():
    """Payload que nao e dict (ex: lista) deve ser rejeitado com erro 4xx.

    FastAPI faz validacao de tipo no parametro `payload: dict` ANTES do corpo
    da rota rodar: retorna 422 (Unprocessable Entity). O guard `isinstance(payload,
    dict)` na rota e uma defesa redundante que nunca e atingida via HTTP normal.
    O comportamento correto de rejeicao (4xx) e o que importa.
    """
    client, _ = build()
    r = client.post("/payments/webhooks/asaas",
                    headers=_wh_headers(),
                    json=["evento-invalido"])
    assert r.status_code in {400, 422}, (
        f"Payload nao-dict deveria ser rejeitado com 400 ou 422, recebeu {r.status_code}"
    )


def test_webhook_missing_event_field_returns_400():
    """Payload dict sem campo 'event' deve retornar 400."""
    client, _ = build()
    r = client.post("/payments/webhooks/asaas",
                    headers=_wh_headers(),
                    json={"payment": {"id": "pay-123"}})
    assert r.status_code == 400
    assert "event" in r.json()["detail"].lower()


def test_webhook_empty_event_returns_400():
    """Campo 'event' vazio deve retornar 400."""
    client, _ = build()
    r = client.post("/payments/webhooks/asaas",
                    headers=_wh_headers(),
                    json={"event": "", "payment": {"id": "pay-123"}})
    assert r.status_code == 400
    assert "event" in r.json()["detail"].lower()


def test_webhook_payment_not_dict_returns_400():
    """Campo 'payment' que nao e dict (ex: numero) deve retornar 400."""
    client, _ = build()
    r = client.post("/payments/webhooks/asaas",
                    headers=_wh_headers(),
                    json={"event": "PAYMENT_RECEIVED", "payment": 12345})
    assert r.status_code == 400
    assert "payment" in r.json()["detail"].lower()


# ------------------------------------------- assinatura fantasma (E) ---

def test_webhook_subscription_ghost_returns_200_noop():
    """external_ref=sub:inexistente → 200 sem criar Payment (noop)."""
    client, db = build()

    r = client.post("/payments/webhooks/asaas",
                    headers=_wh_headers(),
                    json={
                        "event": "PAYMENT_RECEIVED",
                        "payment": {
                            "id": "asaas-pay-ghost",
                            "externalReference": "sub:nao-existe-na-base",
                            "subscription": "sub-asaas-ghost",
                            "value": 99.90,
                            "status": "RECEIVED",
                        },
                    })

    assert r.status_code == 200, r.text
    # Nenhum Payment deve ter sido criado para a assinatura fantasma
    assert db.query(Payment).count() == 0


def test_webhook_subscription_payment_overdue_creates_payment_with_failure_status():
    """PAYMENT_OVERDUE com assinatura valida cria Payment com status=falha_pagamento."""
    from app.models.recurring_plan import RecurringPlan, TutorSubscription

    client, db = build()

    # Cria plano recorrente e assinatura no banco
    plan = RecurringPlan(
        id="plan-001",
        tenant_id=TENANT_ID,
        name="Plano Basico",
        price=99.90,
        walks_per_cycle=4,
        interval="monthly",
        active=True,
    )
    db.add(plan)
    sub = TutorSubscription(
        id="sub-local-001",
        tenant_id=TENANT_ID,
        plan_id="plan-001",
        tutor_id=TUTOR_ID,
        price=99.90,
        walks_per_cycle=4,
        credits_remaining=4,
        asaas_subscription_id="sub-asaas-001",
    )
    db.add(sub)
    db.commit()

    r = client.post("/payments/webhooks/asaas",
                    headers=_wh_headers(),
                    json={
                        "event": "PAYMENT_OVERDUE",
                        "payment": {
                            "id": "asaas-pay-overdue-001",
                            "externalReference": "sub:sub-local-001",
                            "subscription": "sub-asaas-001",
                            "value": 99.90,
                            "status": "OVERDUE",
                        },
                    })

    assert r.status_code == 200, r.text
    db.expire_all()

    # Um Payment local deve ter sido criado com status de falha
    payment = db.query(Payment).filter(
        Payment.provider_payment_id == "asaas-pay-overdue-001"
    ).first()
    assert payment is not None, "Payment local deveria ter sido criado pelo webhook PAYMENT_OVERDUE"
    assert payment.status == "falha_pagamento", (
        f"Status esperado 'falha_pagamento', recebeu '{payment.status}'"
    )
    assert payment.tutor_id == TUTOR_ID
