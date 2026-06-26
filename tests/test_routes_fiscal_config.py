"""Testes de rota admin GET/PUT /{tenant_id}/fiscal-config.

Segue o padrão de test_routes_admin_tenants.py: monta FastAPI mínimo com
o router de fiscal + overrides de get_db / get_current_user, SQLite em memória.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.user import User
from app.routes import fiscal as fiscal_routes

ADMIN_ID = "admin-1"
TUTOR_ID = "tutor-1"
T1 = "tenant-1"
T2 = "tenant-2"


def build(*, current: str = ADMIN_ID):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(User(id=ADMIN_ID, email="admin@test.com", password_hash="x", role="super_admin"))
    db.add(User(id=TUTOR_ID, email="tutor@test.com", password_hash="x", role="tutor"))
    db.add(Tenant(id=T1, name="Tenant One", slug="tenant-one", status="active", plan="pro"))
    db.add(Tenant(id=T2, name="Tenant Two", slug="tenant-two", status="active", plan="pro"))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(fiscal_routes.router)
    test_app.include_router(fiscal_routes.api_router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, current)
    return TestClient(test_app), db


def test_put_then_get_fiscal_config_super_admin():
    client, _ = build()
    r = client.put(f"/admin/tenants/{T1}/fiscal-config", json={"commission_tax_percent": 5})
    assert r.status_code == 200, r.text
    assert r.json()["commission_tax_percent"] == 5
    g = client.get(f"/admin/tenants/{T1}/fiscal-config")
    assert g.status_code == 200, g.text
    assert g.json()["commission_tax_percent"] == 5


def test_get_defaults_zero_when_absent():
    client, _ = build()
    g = client.get(f"/admin/tenants/{T2}/fiscal-config")
    assert g.status_code == 200, g.text
    assert g.json()["commission_tax_percent"] == 0


def test_get_fiscal_config_404_unknown_tenant():
    client, _ = build()
    r = client.get("/admin/tenants/nao-existe/fiscal-config")
    assert r.status_code == 404


def test_put_fiscal_config_forbidden_tutor():
    client, db = build()
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)
    r = client.put(f"/admin/tenants/{T1}/fiscal-config", json={"commission_tax_percent": 5})
    assert r.status_code == 403


def test_put_tax_regime_round_trip():
    """PUT com tax_regime válido aparece no GET seguinte."""
    client, _ = build()
    r = client.put(f"/admin/tenants/{T1}/fiscal-config", json={"tax_regime": "simples_nacional"})
    assert r.status_code == 200, r.text
    assert r.json()["tax_regime"] == "simples_nacional"
    g = client.get(f"/admin/tenants/{T1}/fiscal-config")
    assert g.status_code == 200
    assert g.json()["tax_regime"] == "simples_nacional"


def test_put_tax_regime_invalid_returns_422():
    """PUT com tax_regime inválido retorna 422."""
    client, _ = build()
    r = client.put(f"/admin/tenants/{T1}/fiscal-config", json={"tax_regime": "regime_invalido"})
    assert r.status_code == 422


def test_get_fiscal_config_has_no_simples_nacional_field():
    """A resposta da rota não deve conter o campo legado simples_nacional."""
    client, _ = build()
    g = client.get(f"/admin/tenants/{T1}/fiscal-config")
    assert g.status_code == 200
    assert "simples_nacional" not in g.json()
