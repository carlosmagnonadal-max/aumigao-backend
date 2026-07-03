"""R7 — walk não-garantido até o pagamento liquidar.

Com o gate REQUIRE_PAYMENT_BEFORE_MATCHING ligado, o walk nasce 'awaiting_payment'
e só entra no fluxo operacional quando o webhook de pagamento confirmado o libera.
Default LIGADO (fail-closed — regra do dono). Aqui testamos o gate (puro) e a
liberação no webhook (que só age em walks à espera).
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base, get_db, get_global_db
from app.dependencies.auth import get_current_user
from app.models.payment import Payment
from app.models.pet import Pet
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.routes import payments
from app.routes.walks import _require_payment_before_matching
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-test"
TUTOR_ID = "tutor-test"


def _build():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="t@x.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(Pet(id="pet-1", tutor_id=TUTOR_ID, tenant_id=TENANT_ID, name="Bolinha"))
    db.commit()
    test_app = FastAPI()
    test_app.include_router(payments.router)
    test_app.dependency_overrides[get_db] = lambda: db
    # get_global_db e usado pelo webhook do Asaas; override para ver entidades em memoria.
    test_app.dependency_overrides[get_global_db] = lambda: db
    return test_app, db


def _walk(db, op_status):
    db.add(Walk(id="walk-1", tutor_id=TUTOR_ID, tenant_id=TENANT_ID, pet_id="pet-1",
                scheduled_date="2026-07-01", duration_minutes=30, status="aguardando_pagamento",
                price=100.0, operational_status=op_status))
    db.add(Payment(id="pay-1", tenant_id=TENANT_ID, tutor_id=TUTOR_ID, amount=100.0, walk_id="walk-1",
                   status="pagamento_sandbox_criado", provider="asaas_sandbox", provider_payment_id="prov-1"))
    db.commit()


def _webhook(client, event="PAYMENT_CONFIRMED", prov="prov-1", status="CONFIRMED"):
    return client.post("/payments/webhooks/asaas",
                       json={"event": event, "payment": {"id": prov, "status": status}},
                       headers={"asaas-access-token": "segredo"})


def test_gate_default_on(monkeypatch):
    # Fail-closed: sem env explícito, o gate está LIGADO (regra do dono).
    monkeypatch.delenv("REQUIRE_PAYMENT_BEFORE_MATCHING", raising=False)
    assert _require_payment_before_matching() is True


def test_gate_can_be_disabled(monkeypatch):
    monkeypatch.setenv("REQUIRE_PAYMENT_BEFORE_MATCHING", "false")
    assert _require_payment_before_matching() is False


def test_gate_on(monkeypatch):
    monkeypatch.setenv("REQUIRE_PAYMENT_BEFORE_MATCHING", "true")
    assert _require_payment_before_matching() is True


def test_webhook_confirmed_releases_awaiting_walk(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "segredo")
    test_app, db = _build()
    _walk(db, op_status="awaiting_payment")
    r = _webhook(TestClient(test_app))
    assert r.status_code == 200, r.text
    db.expire_all()
    walk = db.get(Walk, "walk-1")
    assert walk.operational_status == "pending_walker_confirmation"  # liberado p/ matching
    assert walk.status == "Agendado"


def test_webhook_confirmed_noop_when_walk_not_awaiting(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "segredo")
    test_app, db = _build()
    _walk(db, op_status="pending_walker_confirmation")
    _webhook(TestClient(test_app))
    db.expire_all()
    # já estava no fluxo: webhook não rebaixa nem altera o operational_status
    assert db.get(Walk, "walk-1").operational_status == "pending_walker_confirmation"


def test_webhook_overdue_does_not_release_awaiting_walk(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "segredo")
    test_app, db = _build()
    _walk(db, op_status="awaiting_payment")
    _webhook(TestClient(test_app), event="PAYMENT_OVERDUE", status="OVERDUE")
    db.expire_all()
    # sem liquidação, o walk continua à espera (não é garantido)
    assert db.get(Walk, "walk-1").operational_status == "awaiting_payment"
