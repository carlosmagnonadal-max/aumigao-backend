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


def test_contact_post_has_response_model_in_openapi():
    # api-T3: o POST publico declara response_model (contrato {ok, id}) no OpenAPI.
    client, _ = build()
    schema = client.app.openapi()
    op = schema["paths"]["/api/contact"]["post"]
    ref = op["responses"]["201"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("/ContactCreateResponse")
    model = schema["components"]["schemas"]["ContactCreateResponse"]
    assert set(model["properties"]) == {"ok", "id"}
    assert model["properties"]["ok"]["type"] == "boolean"
    assert model["properties"]["id"]["type"] == "string"


def test_contact_post_response_shape_is_exactly_ok_and_id():
    # Campos extras eventualmente retornados pela view sao filtrados pelo modelo.
    client, _ = build()
    r = client.post("/api/contact", json={"name": "Z", "email": "z@test.com"})
    assert r.status_code == 201, r.text
    assert set(r.json()) == {"ok", "id"}


def test_build_contact_email_has_lead_fields():
    from types import SimpleNamespace
    from app.services.contact_notification_service import build_contact_email, DEFAULT_CONTACT_TO

    contact = SimpleNamespace(
        id="c-1", name="Maria", company="Pet Feliz", email="maria@test.com",
        phone="11999", city="SP", business_type="Pet shop",
        interest="Quero contratar White Label", message="Tenho 3 lojas.",
    )
    msg = build_contact_email(contact)
    assert msg["To"] == DEFAULT_CONTACT_TO  # default sem env CONTACT_NOTIFICATION_TO
    assert msg["Reply-To"] == "maria@test.com"
    body = msg.get_content()
    assert "Maria" in body and "Pet Feliz" in body and "maria@test.com" in body
    assert "Tenho 3 lojas." in body
    assert "White Label" in msg["Subject"]
