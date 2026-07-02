"""FIX 8 (P2) — PaymentCreate.amount deve ser > 0; e quando há walk_id, o amount
deve casar com a cotação server-authoritative (build_quote) do passeio.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base, get_db, get_global_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.user import User
from app.models.pet import Pet
from app.models.walk import Walk
from app.routes import payments
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-amt"
TUTOR_ID = "tutor-amt"


@pytest.fixture(autouse=True)
def _sandbox(monkeypatch):
    monkeypatch.setattr(payments, "PAYMENT_MODE", "asaas_sandbox")

    async def _ok(payload, user):
        return {"id": "asaas-1", "status": "PENDING", "invoiceUrl": "x", "bankSlipUrl": None}, {}, "PIX"

    monkeypatch.setattr(payments, "create_asaas_payment", _ok)


def _client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="t@amt.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(Pet(id="pet-a", name="Rex", species="cachorro", tutor_id=TUTOR_ID, tenant_id=TENANT_ID))
    db.add(Walk(id="w-a", tenant_id=TENANT_ID, tutor_id=TUTOR_ID, pet_id="pet-a", price=100.0,
                status="Agendado", scheduled_date="2026-06-10T10:00", duration_minutes=30))
    db.commit()
    app = FastAPI()
    app.include_router(payments.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_global_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)
    return TestClient(app), db


def test_amount_zero_rejected():
    client, _ = _client()
    r = client.post("/payments/create", json={"amount": 0, "method": "pix"})
    assert r.status_code == 422


def test_amount_negative_rejected():
    client, _ = _client()
    r = client.post("/payments/create", json={"amount": -10, "method": "pix"})
    assert r.status_code == 422


def test_walk_amount_matches_quote_ok():
    client, _ = _client()
    r = client.post("/payments/create", json={"walk_id": "w-a", "amount": 100.0, "method": "pix"})
    assert r.status_code == 200, r.text


def test_walk_amount_undercharge_rejected():
    # Subcotação: tutor tenta pagar 50 por um passeio de 100.
    client, _ = _client()
    r = client.post("/payments/create", json={"walk_id": "w-a", "amount": 50.0, "method": "pix"})
    assert r.status_code == 400
    assert "cotação" in r.json()["detail"].lower() or "cotacao" in r.json()["detail"].lower()


def test_walk_amount_overcharge_rejected():
    client, _ = _client()
    r = client.post("/payments/create", json={"walk_id": "w-a", "amount": 150.0, "method": "pix"})
    assert r.status_code == 400
