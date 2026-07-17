"""Cobertura de plano por modalidade (D1, 17/07) + pré-checagem /coverage +
guarda plano_disponivel no create_payment + rota espelho /api/walks.

Padrão da suíte (ver test_recurring_plan_credits.py): SQLite em memória
(StaticPool), FastAPI mínimo por router, overrides de get_db/get_current_user.
Nenhuma chamada de rede real ao Asaas (create_asaas_payment é mockado).
"""
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra as tabelas no Base.metadata
from app.core.database import Base, get_db, get_global_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.pet_tour import PET_TOUR_MODALITY, STANDARD_MODALITY
from app.models.recurring_plan import RecurringPlan
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.models.walk import Walk
from app.routes import payments as payments_module
from app.routes import recurring_plans as plans_module
from app.routes import walks as walks_module
from app.services.payment_split_service import build_quote
from app.services.recurring_plan_service import (
    plan_covers_modality,
    plan_covers_walk_type,
    plan_coverage,
    subscribe,
    walk_subscription_eligible,
)
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-cov"
TUTOR_ID = "tutor-cov"


def _make_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(TenantFeature(tenant_id=TENANT_ID, feature_key="recurring_plans", enabled=True))
    db.add(User(id=TUTOR_ID, email="tutor@cov.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
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


def _pending_subscription(db, tenant, plan):
    """Assinatura ATIVA mas com créditos AINDA NÃO concedidos (aguardando o 1º
    pagamento — credits_granted=False). Reproduz o estado pós subscribe_async."""
    import asyncio

    from app.services.recurring_plan_service import subscribe_async
    return asyncio.run(subscribe_async(db, tenant, TUTOR_ID, plan.id, tutor_user=None))


def _awaiting_walk(db, tenant, *, modality=STANDARD_MODALITY, duration=30):
    walk = Walk(
        id=str(uuid4()), tutor_id=TUTOR_ID, tenant_id=tenant.id, pet_id="pet-1",
        scheduled_date="2026-07-01T10:00:00", duration_minutes=duration, price=50.0,
        status="aguardando_pagamento", operational_status="awaiting_payment",
        modality=modality, subscription_id=None, credit_refunded=False,
    )
    db.add(walk); db.commit(); db.refresh(walk)
    return walk


# ─────────────────────────────────────────────────────────────────────────────
# Regra de modalidade (D1): individual/compartilhado cobertos; pet tour não.
# ─────────────────────────────────────────────────────────────────────────────
def test_plan_covers_modality_standard_and_pet_tour():
    assert plan_covers_modality(STANDARD_MODALITY) is True
    assert plan_covers_modality(None) is True  # default = standard
    assert plan_covers_modality(PET_TOUR_MODALITY) is False


def test_eligible_true_for_standard_walk_with_credits():
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    subscribe(db, tenant, TUTOR_ID, plan.id)
    walk = _awaiting_walk(db, tenant, modality=STANDARD_MODALITY)
    assert plan_covers_walk_type(walk) is True
    assert walk_subscription_eligible(db, walk) is True


def test_eligible_false_for_pet_tour_even_with_credits():
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    subscribe(db, tenant, TUTOR_ID, plan.id)
    walk = _awaiting_walk(db, tenant, modality=PET_TOUR_MODALITY, duration=90)
    assert plan_covers_walk_type(walk) is False
    assert walk_subscription_eligible(db, walk) is False


# ─────────────────────────────────────────────────────────────────────────────
# plan_coverage: os 5 reasons + precedência + metadados.
# ─────────────────────────────────────────────────────────────────────────────
def test_coverage_sem_assinatura():
    db = _make_db(); tenant = _tenant(db)
    result = plan_coverage(db, tenant, TUTOR_ID, "individual")
    assert result == {
        "covered": False,
        "reason": "sem_assinatura",
        "credits_remaining": 0,
        "credits_total": 0,
        "payment_status": None,
        "renewal_at": None,
    }


def test_coverage_assinatura_pendente():
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    _pending_subscription(db, tenant, plan)  # credits_granted=False
    result = plan_coverage(db, tenant, TUTOR_ID, "individual")
    assert result["covered"] is False
    assert result["reason"] == "assinatura_pendente"
    assert result["payment_status"] == "aguardando_pagamento"
    assert result["credits_total"] == 4
    assert result["credits_remaining"] == 0
    assert result["renewal_at"] is not None


def test_coverage_sem_credito():
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    sub = subscribe(db, tenant, TUTOR_ID, plan.id)
    sub.credits_remaining = 0; db.add(sub); db.commit()
    result = plan_coverage(db, tenant, TUTOR_ID, "individual")
    assert result["covered"] is False
    assert result["reason"] == "sem_credito"
    assert result["payment_status"] == "ativa"
    assert result["credits_total"] == 4
    assert result["credits_remaining"] == 0


def test_coverage_ok_individual():
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    subscribe(db, tenant, TUTOR_ID, plan.id)
    result = plan_coverage(db, tenant, TUTOR_ID, "individual")
    assert result["covered"] is True
    assert result["reason"] == "ok"
    assert result["payment_status"] == "ativa"
    assert result["credits_remaining"] == 4
    assert result["credits_total"] == 4
    assert result["renewal_at"] is not None


def test_coverage_ok_shared_is_covered():
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    subscribe(db, tenant, TUTOR_ID, plan.id)
    result = plan_coverage(db, tenant, TUTOR_ID, "shared")
    assert result["covered"] is True
    assert result["reason"] == "ok"


def test_coverage_pet_tour_precedes_all():
    """modalidade_nao_coberta tem a maior precedência: mesmo com assinatura ativa
    e crédito, o pet tour reporta not covered — porém os metadados do plano seguem
    preenchidos (o app pode mostrar 'você tem X créditos, mas o pet tour é à parte')."""
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    subscribe(db, tenant, TUTOR_ID, plan.id)
    result = plan_coverage(db, tenant, TUTOR_ID, "pet_tour")
    assert result["covered"] is False
    assert result["reason"] == "modalidade_nao_coberta"
    assert result["credits_remaining"] == 4  # metadados do plano ativo preservados
    assert result["payment_status"] == "ativa"


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint GET /recurring-plans/coverage
# ─────────────────────────────────────────────────────────────────────────────
def _make_plans_client(db):
    app_t = FastAPI()
    app_t.include_router(plans_module.router)
    app_t.dependency_overrides[get_db] = lambda: db
    app_t.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)
    return TestClient(app_t)


def test_coverage_endpoint_ok_shape():
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    subscribe(db, tenant, TUTOR_ID, plan.id)
    client = _make_plans_client(db)

    resp = client.get("/recurring-plans/coverage", params={"walk_type": "individual", "duration": 30})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["covered"] is True and body["reason"] == "ok"
    assert set(body.keys()) == {
        "covered", "reason", "credits_remaining", "credits_total", "payment_status", "renewal_at",
    }


def test_coverage_endpoint_pet_tour_not_covered():
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    subscribe(db, tenant, TUTOR_ID, plan.id)
    client = _make_plans_client(db)
    resp = client.get("/recurring-plans/coverage", params={"walk_type": "pet_tour"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["reason"] == "modalidade_nao_coberta"


def test_coverage_endpoint_invalid_walk_type_400():
    db = _make_db()
    client = _make_plans_client(db)
    resp = client.get("/recurring-plans/coverage", params={"walk_type": "banho"})
    assert resp.status_code == 400, resp.text


# ─────────────────────────────────────────────────────────────────────────────
# confirm-plan de pet tour recusado (409 modalidade_nao_coberta)
# ─────────────────────────────────────────────────────────────────────────────
def _make_walks_client(db, *, mirror=False):
    app_t = FastAPI()
    app_t.include_router(walks_module.api_router if mirror else walks_module.router)
    app_t.dependency_overrides[get_db] = lambda: db
    app_t.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)
    return TestClient(app_t)


def test_confirm_plan_pet_tour_rejected():
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    sub = subscribe(db, tenant, TUTOR_ID, plan.id)
    walk = _awaiting_walk(db, tenant, modality=PET_TOUR_MODALITY, duration=90)
    client = _make_walks_client(db)

    resp = client.post(f"/walks/{walk.id}/confirm-plan")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "modalidade_nao_coberta"
    # Não debitou crédito nem vinculou a assinatura.
    db.refresh(sub)
    assert sub.credits_remaining == 4
    assert db.get(Walk, walk.id).subscription_id is None


def test_confirm_plan_standard_via_api_mirror():
    """Rota espelho /api/walks: confirm-plan responde e debita normalmente."""
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    sub = subscribe(db, tenant, TUTOR_ID, plan.id)
    walk = _awaiting_walk(db, tenant, modality=STANDARD_MODALITY)
    client = _make_walks_client(db, mirror=True)

    resp = client.post(f"/api/walks/{walk.id}/confirm-plan")
    assert resp.status_code == 200, resp.text
    assert db.get(Walk, walk.id).subscription_id == sub.id
    db.refresh(sub)
    assert sub.credits_remaining == 3


# ─────────────────────────────────────────────────────────────────────────────
# Guarda plano_disponivel no create_payment
# ─────────────────────────────────────────────────────────────────────────────
def _make_payments_client(db, monkeypatch):
    monkeypatch.setattr(payments_module, "PAYMENT_MODE", "asaas_sandbox")

    async def _fake_create(payload, user):
        return ({"id": "asaas-pay-1", "status": "PENDING", "invoiceUrl": "https://inv", "bankSlipUrl": None}, {}, "PIX")

    monkeypatch.setattr(payments_module, "create_asaas_payment", _fake_create)

    app_t = FastAPI()
    app_t.include_router(payments_module.router)
    app_t.dependency_overrides[get_db] = lambda: db
    app_t.dependency_overrides[get_global_db] = lambda: db
    app_t.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)
    return TestClient(app_t)


def test_create_payment_offers_plan_without_flag(monkeypatch):
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    subscribe(db, tenant, TUTOR_ID, plan.id)
    walk = _awaiting_walk(db, tenant, modality=STANDARD_MODALITY)
    client = _make_payments_client(db, monkeypatch)

    resp = client.post("/payments/create", json={"walk_id": walk.id, "amount": 50.0, "method": "pix"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "plano_disponivel"


def test_create_payment_charge_anyway_bypasses_plan(monkeypatch):
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    subscribe(db, tenant, TUTOR_ID, plan.id)
    walk = _awaiting_walk(db, tenant, modality=STANDARD_MODALITY)
    client = _make_payments_client(db, monkeypatch)

    amount = round(float(build_quote(db, walk.tenant_id, walk.price)["total"]), 2)
    resp = client.post(
        "/payments/create",
        json={"walk_id": walk.id, "amount": amount, "method": "pix", "charge_anyway": True},
    )
    assert resp.status_code == 200, resp.text
    from app.models.payment import Payment
    assert db.query(Payment).filter(Payment.walk_id == walk.id).count() == 1


def test_create_payment_charges_when_not_eligible(monkeypatch):
    """Passeio sem plano elegível (tutor sem assinatura) cobra normalmente, sem
    o 409 plano_disponivel, mesmo sem charge_anyway."""
    db = _make_db(); tenant = _tenant(db)
    walk = _awaiting_walk(db, tenant, modality=STANDARD_MODALITY)  # sem assinatura
    client = _make_payments_client(db, monkeypatch)

    amount = round(float(build_quote(db, walk.tenant_id, walk.price)["total"]), 2)
    resp = client.post("/payments/create", json={"walk_id": walk.id, "amount": amount, "method": "pix"})
    assert resp.status_code == 200, resp.text
    from app.models.payment import Payment
    assert db.query(Payment).filter(Payment.walk_id == walk.id).count() == 1
