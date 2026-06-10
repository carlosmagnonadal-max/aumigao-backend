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
