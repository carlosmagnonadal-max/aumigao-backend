"""Testes de ROTA (camada HTTP) das features da Onda 1.

Os outros testes cobrem os services; estes cobrem o wiring real: response_model,
serialização (ex.: pet_name/tutor_count), gating de feature via endpoint e status
HTTP. Monta um FastAPI mínimo só com os routers de cliente + overrides de get_db /
get_current_user (SQLite em memória) — NÃO importa app.main (que conecta no Neon).
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.routes import pet_tour, recurring_plans, shared_walks
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-test"
TUTOR_ID = "tutor-test"


def build(*, features: set[str] | None = None, pets: list[str] | None = None):
    # StaticPool: uma única conexão compartilhada — senão cada thread do TestClient
    # abre um SQLite em memória vazio (tabelas somem).
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # slug = DEFAULT para get_default_tenant resolver este tenant sem criar outro.
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="tutor@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    for key in features or set():
        db.add(TenantFeature(tenant_id=TENANT_ID, feature_key=key, enabled=True))
    for pid in pets or []:
        db.add(Pet(id=pid, tutor_id=TUTOR_ID, name=pid, can_walk_with_other_pets=True))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(recurring_plans.router)
    test_app.include_router(pet_tour.router)
    test_app.include_router(shared_walks.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)
    return TestClient(test_app), db


# ----- recurring plans -----
def test_recurring_plans_gated_off():
    client, _ = build(features=set())
    r = client.get("/recurring-plans")
    assert r.status_code == 200
    assert r.json()["available"] is False


def test_recurring_plans_available():
    client, _ = build(features={"recurring_plans"})
    r = client.get("/recurring-plans")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["plans"] == []
    assert body["subscription"] is None


# ----- pet tour -----
def test_pet_tour_gated_off():
    client, _ = build(features=set())
    assert client.get("/pet-tour").json()["available"] is False


def test_pet_tour_available_has_price():
    client, _ = build(features={"pet_tour"})
    body = client.get("/pet-tour").json()
    assert body["available"] is True
    assert body["base_price"] is not None
    assert body["min_duration_minutes"] >= 61


# ----- shared walks -----
def test_shared_walks_gated_off():
    client, _ = build(features=set())
    assert client.get("/shared-walks").json()["available"] is False


def test_shared_walks_create_serializes_participants():
    client, _ = build(features={"shared_walks"}, pets=["rex", "mel"])
    r = client.post("/shared-walks", json={
        "scheduled_date": "2026-07-01T10:00:00",
        "duration_minutes": 45,
        "host_pet_ids": ["rex", "mel"],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "forming"
    assert body["tutor_count"] == 1
    names = sorted(p["pet_name"] for p in body["participants"])
    assert names == ["mel", "rex"]  # serialização de pet_name funciona


def test_shared_walks_create_blocked_without_feature():
    client, _ = build(features=set(), pets=["rex"])
    r = client.post("/shared-walks", json={
        "scheduled_date": "2026-07-01T10:00:00", "duration_minutes": 45, "host_pet_ids": ["rex"],
    })
    assert r.status_code == 403


def test_shared_walks_create_then_get_and_checkout():
    client, _ = build(features={"shared_walks"}, pets=["rex"])
    created = client.post("/shared-walks", json={
        "scheduled_date": "2026-07-01T10:00:00", "duration_minutes": 45, "host_pet_ids": ["rex"],
    }).json()
    walk_id = created["id"]
    assert client.get(f"/shared-walks/{walk_id}").status_code == 200
    paid = client.post(f"/shared-walks/{walk_id}/checkout").json()
    assert all(p["status"] == "paid" for p in paid["participants"])


# ----- fluxo POPULADO ponta a ponta (flag ligada) -----
def test_recurring_plans_subscribe_happy_path():
    from app.models.recurring_plan import RecurringPlan, TutorSubscription
    from app.services.recurring_plan_service import grant_credits_on_payment
    client, db = build(features={"recurring_plans"})
    db.add(RecurringPlan(id="plan1", tenant_id=TENANT_ID, name="Mensal 8", price=99.0, walks_per_cycle=8, active=True))
    db.commit()
    view = client.get("/recurring-plans").json()
    assert view["available"] is True and len(view["plans"]) == 1
    sub = client.post("/recurring-plans/plan1/subscribe").json()
    # COMPORTAMENTO GATED (anti passeio-grátis): créditos só são concedidos após
    # confirmação do 1º pagamento via webhook. Imediatamente após assinar,
    # credits_remaining == 0 (pendente de pagamento).
    assert sub["status"] == "active"
    assert sub["credits_remaining"] == 0, (
        "subscribe_async deve criar assinatura com créditos zerados (gate anti passeio-grátis); "
        "créditos só são concedidos via grant_credits_on_payment ao confirmar o 1º pagamento"
    )
    assert sub["plan_name"] == "Mensal 8"
    # GET agora reflete a assinatura ativa
    assert client.get("/recurring-plans").json()["subscription"]["plan_id"] == "plan1"

    # Verifica que grant_credits_on_payment concede os créditos do ciclo
    # (simula confirmação do 1º pagamento, normalmente acionada pelo webhook).
    sub_obj = db.query(TutorSubscription).filter(TutorSubscription.tutor_id == TUTOR_ID).first()
    assert sub_obj is not None
    granted = grant_credits_on_payment(db, sub_obj)
    db.commit()
    db.refresh(sub_obj)
    assert granted is True
    assert sub_obj.credits_remaining == 8
    assert sub_obj.credits_granted is True
    # Idempotência: segunda chamada não concede novamente
    assert grant_credits_on_payment(db, sub_obj) is False


def test_shared_walk_full_lifecycle_two_tutors():
    client, db = build(features={"shared_walks"}, pets=["rex"])
    # convidado: 2o tutor + pet apto a compartilhar
    db.add(User(id="guest", email="guest@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(Pet(id="mel", tutor_id="guest", name="mel", can_walk_with_other_pets=True))
    db.commit()

    created = client.post("/shared-walks", json={
        "scheduled_date": "2026-07-01T10:00:00", "duration_minutes": 45, "host_pet_ids": ["rex"],
    }).json()
    wid = created["id"]

    # convidado entra (troca o usuário autenticado)
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, "guest")
    joined = client.post(f"/shared-walks/{wid}/join", json={"pet_id": "mel"}).json()
    assert joined["tutor_count"] == 2
    client.post(f"/shared-walks/{wid}/checkout")  # convidado paga

    # host volta, paga e confirma
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)
    client.post(f"/shared-walks/{wid}/checkout")
    confirmed = client.post(f"/shared-walks/{wid}/confirm").json()
    assert confirmed["status"] == "confirmed"
