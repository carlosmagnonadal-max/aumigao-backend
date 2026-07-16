import os
from datetime import datetime, timedelta

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # registra todas as tabelas no Base
from app.core.database import Base, get_db, get_global_db
from app.dependencies.auth import get_current_user
from app.routes import payments as payments_module
from app.routes import walks as walks_module
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.models.pet import Pet
from app.models.walk import Walk, WalkMatchingAttempt
from app.models.walker_profile import WalkerProfile
from app.models.tenant_walker_access import TenantWalkerAccess
from app.models.payment import Payment
from app.models.recurring_plan import (
    RecurringPlan, TutorSubscription, SUBSCRIPTION_ACTIVE,
)
from app.services.recurring_plan_service import (
    subscribe, get_active_subscription, consume_credit_if_available,
    refund_credit_for_walk, reset_credits_if_renewal,
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


def test_refund_returns_credit_for_current_cycle():
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    sub = subscribe(db, tenant, TUTOR_ID, plan.id)
    consume_credit_if_available(db, tenant, TUTOR_ID); db.commit()  # 4 -> 3
    walk = _make_covered_walk(db, tenant, sub)

    assert refund_credit_for_walk(db, walk) is True
    db.commit()
    db.refresh(sub)
    assert sub.credits_remaining == 4
    assert walk.credit_refunded is True


def test_refund_is_idempotent():
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    sub = subscribe(db, tenant, TUTOR_ID, plan.id)
    walk = _make_covered_walk(db, tenant, sub)
    refund_credit_for_walk(db, walk); db.commit()
    assert refund_credit_for_walk(db, walk) is False  # já estornado


def test_refund_skips_previous_cycle():
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    sub = subscribe(db, tenant, TUTOR_ID, plan.id)
    walk = _make_covered_walk(db, tenant, sub, created_at=datetime.utcnow() - timedelta(days=40))
    assert refund_credit_for_walk(db, walk) is False


def test_refund_skips_walk_without_subscription():
    db = _make_db(); tenant = _tenant(db)
    walk = Walk(
        id="walk-avulso", tutor_id=TUTOR_ID, tenant_id=tenant.id, pet_id="pet-1",
        scheduled_date="2026-07-01", duration_minutes=30, price=50.0,
        status="Agendado", subscription_id=None, credit_refunded=False,
    )
    db.add(walk); db.commit()
    assert refund_credit_for_walk(db, walk) is False


def test_reset_refills_on_genuine_renewal():
    # Nota: with_for_update() em reset_credits_if_renewal é no-op no SQLite dos testes;
    # a serialização real contra webhooks duplicados só vale em PostgreSQL (prod).
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    sub = subscribe(db, tenant, TUTOR_ID, plan.id)
    sub.credits_remaining = 1
    sub.current_period_end = datetime.utcnow() - timedelta(days=1)
    db.add(sub); db.commit()

    assert reset_credits_if_renewal(db, sub) is True
    db.commit(); db.refresh(sub)
    assert sub.credits_remaining == 4
    assert sub.current_period_end > datetime.utcnow()


def test_reset_skips_when_period_current():
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    sub = subscribe(db, tenant, TUTOR_ID, plan.id)
    sub.credits_remaining = 2
    sub.current_period_end = datetime.utcnow() + timedelta(days=10)
    db.add(sub); db.commit()

    assert reset_credits_if_renewal(db, sub) is False
    db.refresh(sub)
    assert sub.credits_remaining == 2


def test_create_walk_consumes_credit():
    """Teste unitário: cria Walk diretamente e verifica que consume_credit_if_available
    decrementa os créditos e que walk.subscription_id é setado.

    Caminho unitário escolhido porque o endpoint POST /walks depende do
    TenantResolverMiddleware (request.state.tenant_id) que não é emulável num
    TestClient isolado sem montar a app completa com middleware.
    A lógica de produção é exatamente: consume_credit_if_available → walk.subscription_id = sub.id.
    """
    from uuid import uuid4

    db = _make_db()
    tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    subscribe(db, tenant, TUTOR_ID, plan.id)

    # Simula o que create_walk faz após db.add(walk)
    walk = Walk(
        id=str(uuid4()),
        tutor_id=TUTOR_ID,
        tenant_id=tenant.id,
        pet_id="pet-1",
        scheduled_date="2026-07-01",
        duration_minutes=30,
        price=50.0,
        status="Agendado",
        subscription_id=None,
        credit_refunded=False,
    )
    db.add(walk)

    # Bloco equivalente ao inserido em walks.py (Step 2)
    _covered_by_subscription = False
    if walk.tenant_id:
        _t = db.get(type(tenant), walk.tenant_id)
        if _t is not None:
            _sub = consume_credit_if_available(db, _t, walk.tutor_id)
            if _sub is not None:
                walk.subscription_id = _sub.id
                _covered_by_subscription = True

    db.commit()

    assert _covered_by_subscription is True

    sub = get_active_subscription(db, tenant.id, TUTOR_ID)
    assert sub is not None
    assert sub.credits_remaining == 3
    assert db.get(Walk, walk.id).subscription_id == sub.id


def _make_payments_client(db):
    app_t = FastAPI()
    app_t.include_router(payments_module.router)
    app_t.dependency_overrides[get_db] = lambda: db
    app_t.dependency_overrides[get_global_db] = lambda: db   # webhook usa get_global_db
    app_t.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)
    return TestClient(app_t)


def _make_walks_client(db):
    app_t = FastAPI()
    app_t.include_router(walks_module.router)
    app_t.dependency_overrides[get_db] = lambda: db
    app_t.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)
    return TestClient(app_t)


def test_payment_create_rejected_for_covered_walk():
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    sub = subscribe(db, tenant, TUTOR_ID, plan.id)
    walk = _make_covered_walk(db, tenant, sub)
    client = _make_payments_client(db)

    resp = client.post("/payments/create", json={"walk_id": walk.id, "amount": 50.0, "method": "pix"})
    assert resp.status_code == 409, resp.text


import app.routes.admin as admin_mod
from app.routes.admin import _ensure_internal_walk_payment


def test_internal_payment_passes_walker_id(monkeypatch):
    db = _make_db(); tenant = _tenant(db)
    captured = {}

    def fake_split(db, tenant_id, amount, *, walker_id=None):
        captured["walker_id"] = walker_id
        return {"commission_percent": 10.0, "platform_amount": amount * 0.1, "walker_amount": amount * 0.9}

    monkeypatch.setattr(admin_mod, "build_payment_split", fake_split)

    db.add(User(id="walker-xyz", email="w@x.com", password_hash="x", role="passeador", tenant_id=tenant.id))
    walk = Walk(
        id="walk-internal", tutor_id=TUTOR_ID, tenant_id=tenant.id, pet_id="pet-1",
        scheduled_date="2026-07-01", duration_minutes=30, price=100.0,
        status="Agendado", walker_id="walker-xyz",
    )
    db.add(walk); db.commit()

    _ensure_internal_walk_payment(walk, db)   # ordem real: (walk, db)

    assert captured["walker_id"] == "walker-xyz"
    pay = db.query(Payment).filter(Payment.walk_id == "walk-internal").first()
    assert pay is not None and pay.walker_amount == 90.0


from app.services.operational_matching_service import update_operational_status


def test_cancel_walk_refunds_credit():
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    sub = subscribe(db, tenant, TUTOR_ID, plan.id)
    consume_credit_if_available(db, tenant, TUTOR_ID); db.commit()  # 4 -> 3
    walk = _make_covered_walk(db, tenant, sub)

    # "Cancelado" é a string legada que LEGACY_STATUS_TO_OPERATIONAL traduz para RIDE_CANCELLED
    update_operational_status(walk, "Cancelado", db)
    db.commit()

    db.refresh(sub)
    assert sub.credits_remaining == 4
    assert walk.credit_refunded is True


def test_delete_walk_refunds_credit():
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    sub = subscribe(db, tenant, TUTOR_ID, plan.id)
    consume_credit_if_available(db, tenant, TUTOR_ID); db.commit()  # 4 -> 3
    walk = _make_covered_walk(db, tenant, sub)
    # operational_status padrão ("ride_scheduled") está no conjunto bloqueado de deleção;
    # muda para "pending_walker_confirmation" para que o DELETE seja permitido.
    walk.operational_status = "pending_walker_confirmation"
    db.commit()
    client = _make_walks_client(db)

    resp = client.delete(f"/walks/{walk.id}")
    assert resp.status_code in (200, 204), resp.text

    db.refresh(sub)
    assert sub.credits_remaining == 4


def test_subscription_walk_payment_uses_distinct_provider_and_excluded_from_revenue():
    import app.routes.admin as admin_mod
    from app.routes.admin import _ensure_internal_walk_payment
    from sqlalchemy import or_, func

    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    sub = subscribe(db, tenant, TUTOR_ID, plan.id)
    db.add(User(id="walker-z", email="wz@x.com", password_hash="x", role="passeador", tenant_id=tenant.id))
    walk = _make_covered_walk(db, tenant, sub)   # tem subscription_id
    walk.walker_id = "walker-z"; db.add(walk); db.commit()

    _ensure_internal_walk_payment(walk, db)
    db.commit()

    pay = db.query(Payment).filter(Payment.walk_id == walk.id).first()
    assert pay is not None
    assert pay.provider == "subscription_walk"
    # walker recebe (walker_amount registrado)
    assert pay.walker_amount is not None and pay.walker_amount > 0
    # excluído das somas de receita (replica o filtro usado nos relatórios)
    revenue = db.query(func.coalesce(func.sum(Payment.amount), 0.0)).filter(
        or_(Payment.provider.is_(None), Payment.provider != "subscription_walk")
    ).scalar()
    assert revenue == 0.0  # o único Payment é o subscription_walk → excluído


def test_subscription_walk_split_anchors_walker_and_uses_effective_amount():
    """Economia do plano (07/07): walker recebe o residual da ÂNCORA cheia
    (walk.price), e Payment.amount = valor EFETIVO do plano (preço÷passeios) —
    receita verdadeira, sem fantasma do avulso."""
    from app.routes.admin import _ensure_internal_walk_payment

    db = _make_db(); tenant = _tenant(db)
    # Plano: 4 passeios por R$170 → efetivo R$42,50/passeio (15% off da âncora 50).
    plan = _make_plan(db, tenant, walks_per_cycle=4, price=170.0)
    sub = subscribe(db, tenant, TUTOR_ID, plan.id)
    db.add(User(id="walker-anchor", email="wa@x.com", password_hash="x", role="passeador", tenant_id=tenant.id))
    walk = _make_covered_walk(db, tenant, sub)  # walk.price (âncora) = 50.0
    walk.walker_id = "walker-anchor"; db.add(walk); db.commit()

    _ensure_internal_walk_payment(walk, db)
    db.commit()

    pay = db.query(Payment).filter(Payment.walk_id == walk.id).first()
    assert pay is not None
    # Receita registrada = efetivo do plano, não a âncora (mata receita-fantasma).
    assert pay.amount == pytest.approx(42.50, abs=0.01)
    # Walker intocado em reais: residual da ÂNCORA cheia (walk.price=50), com a
    # comissão/margem REAIS resolvidas — idêntico ao avulso, MAIOR que o efetivo.
    expected_walker = 50.0 * (100 - pay.commission_percent) / 100
    assert pay.walker_amount == pytest.approx(expected_walker, abs=0.01)
    assert pay.walker_amount > pay.amount
    # Plano abaixo do piso (15% off > fatias disponíveis): plataforma NUNCA
    # negativa — zera; o déficit é do tenant que precificou (visível em relatório).
    assert pay.platform_amount == pytest.approx(0.0, abs=0.01)


def test_subscription_renewal_resets_credits(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "tok-credits")
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    sub = subscribe(db, tenant, TUTOR_ID, plan.id)
    sub.credits_remaining = 0
    sub.current_period_end = datetime.utcnow() - timedelta(days=1)
    sub.asaas_subscription_id = "asaas-sub-1"
    db.add(sub); db.commit()
    client = _make_payments_client(db)

    payload = {
        "event": "PAYMENT_RECEIVED",
        "payment": {
            "id": "pay-renew-1", "status": "RECEIVED",
            "externalReference": f"sub:{sub.id}", "subscription": "asaas-sub-1",
        },
    }
    resp = client.post("/payments/webhooks/asaas", json=payload,
                       headers={"asaas-access-token": "tok-credits"})
    assert resp.status_code == 200, resp.text

    db.refresh(sub)
    assert sub.credits_remaining == 4
    assert sub.current_period_end > datetime.utcnow()


from app.services.recurring_plan_service import grant_credits_on_payment


def test_async_subscribe_does_not_grant_credits_before_payment():
    import asyncio
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    # tutor_user=None → subscribe_async pula o Asaas e commita a assinatura local
    from app.services.recurring_plan_service import subscribe_async
    sub = asyncio.run(subscribe_async(db, tenant, TUTOR_ID, plan.id, tutor_user=None))
    assert sub.credits_remaining == 0
    assert sub.credits_granted is False
    # crédito não usável antes do pagamento
    assert consume_credit_if_available(db, tenant, TUTOR_ID) is None


def test_grant_credits_on_first_payment():
    import asyncio
    from app.services.recurring_plan_service import subscribe_async
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    sub = asyncio.run(subscribe_async(db, tenant, TUTOR_ID, plan.id, tutor_user=None))

    assert grant_credits_on_payment(db, sub) is True
    db.commit(); db.refresh(sub)
    assert sub.credits_remaining == 4 and sub.credits_granted is True
    # idempotente: 2ª vez não concede de novo
    assert grant_credits_on_payment(db, sub) is False
    # agora consome normalmente
    assert consume_credit_if_available(db, tenant, TUTOR_ID) is not None


WALKER_E_ID = "walker-eligible"


def _seed_eligible_walker(db, tenant, walker_id=WALKER_E_ID):
    """Passeador elegivel para o pool do tenant (mesmo padrao de test_routes_matching):
    User(role=walker) + WalkerProfile ativo + TenantWalkerAccess ativo na rede."""
    db.add(User(id=walker_id, email=f"{walker_id}@x.com", password_hash="x",
                role="walker", tenant_id=tenant.id, is_active=True))
    db.add(WalkerProfile(
        id=f"profile-{walker_id}", user_id=walker_id, full_name="Passeador Elegivel",
        status="active", active_as_walker=True, city="salvador",
        created_at=datetime.utcnow(),
    ))
    db.add(TenantWalkerAccess(
        id=f"twa-{walker_id}", tenant_id=tenant.id, walker_user_id=walker_id,
        status="active", access_type="shared_network",
    ))
    db.commit()


def _subscription_walk_payload(**extra):
    base = {
        "pet_id": "pet-1",
        "scheduled_date": "2026-07-01T10:00:00",
        "duration_minutes": 30,
        "price": 40.0,
        "pickup_method": "Buscar em casa",
        "address_snapshot": "Rua A, 100 - Centro",
        "notes": "",
    }
    base.update(extra)
    return base


def test_create_walk_covered_by_subscription_enters_matching_queue(monkeypatch):
    """BUG passeio orfao de plano mensal: passeio coberto por credito de assinatura
    NAO tem webhook Asaas para libera-lo; o matching precisa disparar no proprio
    create. Apos POST /walks, deve existir uma WalkMatchingAttempt (o passeio entra
    na fila /walker/requests). Usa modo exclusivo (only_selected) para a criacao da
    tentativa ser deterministica, respeitando o passeador escolhido pelo tutor.
    Gate LIGADO (cenario de producao): so a cobertura de assinatura libera o passeio."""
    monkeypatch.setenv("REQUIRE_PAYMENT_BEFORE_MATCHING", "true")
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    subscribe(db, tenant, TUTOR_ID, plan.id)  # concede 4 creditos
    _seed_eligible_walker(db, tenant)
    client = _make_walks_client(db)

    resp = client.post("/walks", json=_subscription_walk_payload(
        walker_id=WALKER_E_ID, walker_selection_mode="only_selected",
    ))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    walk_id = body["id"]

    # Passeio coberto pela assinatura (sem cobranca avulsa) e nasce liberado.
    assert db.get(Walk, walk_id).subscription_id is not None
    assert body["operational_status"] == "pending_walker_confirmation"

    # O passeio ENTROU na fila: existe uma tentativa de matching pendente para o
    # passeador escolhido (exigencia de /walker/requests).
    attempt = (
        db.query(WalkMatchingAttempt)
        .filter(WalkMatchingAttempt.walk_id == walk_id)
        .first()
    )
    assert attempt is not None, "passeio de assinatura ficou orfao (nenhuma WalkMatchingAttempt)"
    assert attempt.walker_id == WALKER_E_ID  # modo exclusivo respeitado
    assert attempt.status == "pending"
    assert db.get(Walk, walk_id).assigned_walker_id == WALKER_E_ID


def test_create_walk_awaiting_payment_does_not_enter_matching(monkeypatch):
    """Nao-regressao: com o gate ligado e SEM assinatura, o passeio nasce
    'awaiting_payment' e NAO cria matching no create — a liberacao continua indo
    pelo webhook do Asaas (payments.py), sem duplicar."""
    monkeypatch.setenv("REQUIRE_PAYMENT_BEFORE_MATCHING", "true")
    db = _make_db(); tenant = _tenant(db)
    _seed_eligible_walker(db, tenant)  # ha passeador disponivel, mas nao deve ser acionado
    client = _make_walks_client(db)

    resp = client.post("/walks", json=_subscription_walk_payload())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    walk_id = body["id"]

    assert body["operational_status"] == "awaiting_payment"
    assert db.get(Walk, walk_id).subscription_id is None
    attempt = (
        db.query(WalkMatchingAttempt)
        .filter(WalkMatchingAttempt.walk_id == walk_id)
        .first()
    )
    assert attempt is None, "awaiting_payment nao deve criar matching no create (vai pelo webhook)"


def test_refund_double_call_same_walk_no_double_credit():
    db = _make_db(); tenant = _tenant(db)
    plan = _make_plan(db, tenant, walks_per_cycle=4)
    sub = subscribe(db, tenant, TUTOR_ID, plan.id)
    consume_credit_if_available(db, tenant, TUTOR_ID); db.commit()  # 4 -> 3
    walk = _make_covered_walk(db, tenant, sub)

    assert refund_credit_for_walk(db, walk) is True
    db.commit()
    assert refund_credit_for_walk(db, walk) is False  # 2ª chamada não estorna de novo
    db.commit(); db.refresh(sub)
    assert sub.credits_remaining == 4  # voltou exatamente 1 crédito, não 2
