"""FIX 3 (P1) — request_withdrawal race: dois pedidos concorrentes com saldo para
UM saque não podem ambos criar o saque (double-spend).

O fix adiciona SELECT ... FOR UPDATE numa linha-âncora (o User do walker) no topo
do endpoint, serializando pedidos concorrentes; a revalidação de saldo já desconta
saques pendentes. SQLite (testes) não faz locking real, então validamos:
  (1) o lock é solicitado (with_for_update);
  (2) dois pedidos sequenciais com saldo p/ 1 -> o 2º é rejeitado (saldo insuficiente),
      só existe 1 saque pendente.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.models.pet import Pet
from app.models.walk import Walk
from app.models.payment import Payment
from app.models.walker_profile import WalkerProfile
from app.routes import walker as walker_module

WALKER_ID = "k1"
TUTOR_ID = "tut1"
PET_ID = "pet1"
TENANT_ID = "t1"


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(User(id=WALKER_ID, email="k1@x.com", full_name="K", role="walker", password_hash="x"))
    db.add(User(id=TUTOR_ID, email="tut@x.com", full_name="T", role="tutor", password_hash="x"))
    db.add(Pet(id=PET_ID, name="Rex", species="cachorro", tutor_id=TUTOR_ID, tenant_id=TENANT_ID))
    db.add(WalkerProfile(id="wp1", user_id=WALKER_ID, pix_key="k1@pix.com", status="approved"))
    # Crédito de R$ 100 (1 passeio finalizado pago).
    db.add(Walk(id="w1", tenant_id=TENANT_ID, tutor_id=TUTOR_ID, walker_id=WALKER_ID, pet_id=PET_ID,
                price=100.0, status="Finalizado", scheduled_date="2026-06-10T10:00", duration_minutes=30))
    db.add(Payment(id="p-w1", tenant_id=TENANT_ID, tutor_id=TUTOR_ID, walk_id="w1", amount=100.0,
                   status="paid", provider="internal", walker_amount=100.0))
    db.commit()
    return db


def _client(db):
    app = FastAPI()
    app.include_router(walker_module.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, WALKER_ID)
    return TestClient(app)


def test_two_sequential_withdrawals_only_one_succeeds():
    db = _db()
    client = _client(db)

    r1 = client.post("/walker/withdrawals", json={"amount": 100})
    assert r1.status_code == 200, r1.text

    # Segundo pedido: o saque pendente já consumiu o saldo -> insuficiente.
    r2 = client.post("/walker/withdrawals", json={"amount": 100})
    assert r2.status_code == 400
    assert "insuficiente" in r2.json()["detail"].lower()

    pend = db.query(Payment).filter(Payment.provider == "pix", Payment.amount < 0).count()
    assert pend == 1


def test_withdrawal_acquires_for_update_lock(monkeypatch):
    db = _db()
    called = {"n": 0}
    real_query = db.query

    def spy_query(*a, **k):
        q = real_query(*a, **k)
        orig = q.with_for_update

        def wrapped(*aa, **kk):
            called["n"] += 1
            return orig(*aa, **kk)

        q.with_for_update = wrapped
        return q

    db.query = spy_query
    client = _client(db)
    r = client.post("/walker/withdrawals", json={"amount": 50})
    assert r.status_code == 200, r.text
    assert called["n"] >= 1  # lock âncora solicitado
