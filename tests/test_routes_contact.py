"""Testes de ROTA do intake público de contato (app/routes/contact.py).

Padrão do projeto: FastAPI mínimo com só o router de contact, SQLite em memória,
override de get_db. NÃO importa app.main (que conecta no banco de produção).
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.models.contact_message import ContactMessage
from app.routes import contact


def build():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    test_app = FastAPI()
    test_app.include_router(contact.router)
    test_app.dependency_overrides[get_db] = lambda: db
    return TestClient(test_app), db


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    contact.contact_rate_limiter._failures.clear()
    yield
    contact.contact_rate_limiter._failures.clear()


def test_contact_persists_message():
    client, db = build()
    r = client.post(
        "/api/contact",
        json={
            "name": "Maria",
            "company": "Pet Feliz",
            "email": "maria@test.com",
            "interest": "Quero contratar White Label",
            "message": "Tenho 3 lojas e quero o app com minha marca.",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["id"]
    row = db.query(ContactMessage).one()
    assert row.email == "maria@test.com"
    assert row.company == "Pet Feliz"
    assert row.source == "site"
    assert row.status == "new"


def test_contact_rejects_invalid_email():
    client, _ = build()
    r = client.post("/api/contact", json={"name": "X", "email": "nao-eh-email"})
    assert r.status_code == 400


def test_contact_requires_name_or_company():
    client, _ = build()
    r = client.post("/api/contact", json={"email": "anon@test.com"})
    assert r.status_code == 400


def test_contact_rate_limit_blocks_flood():
    client, _ = build()
    # CONTACT_RATE_LIMIT default = 10; a 11ª deve bloquear (429).
    last = None
    for i in range(12):
        last = client.post("/api/contact", json={"name": f"u{i}", "email": f"u{i}@test.com"})
    assert last.status_code == 429, last.text
