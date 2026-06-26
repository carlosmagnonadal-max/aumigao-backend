"""Testes de rota admin GET /{tenant_id}/financial-summary e /{payment_id}/provision.

Segue o padrão de test_routes_fiscal_config.py: FastAPI mínimo com SQLite in-memory.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.user import User
from app.routes import fiscal as fiscal_routes
from app.services import fiscal_config_service as cfg_svc
from app.services import provision_service as prov_svc

ADMIN_ID = "admin-1"
T1 = "tenant-1"
T2 = "tenant-2"


class FakePayment:
    def __init__(self, id, amount, platform_amount=None, walker_amount=None):
        self.id = id; self.amount = amount
        self.platform_amount = platform_amount; self.walker_amount = walker_amount


def build(*, current: str = ADMIN_ID):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(User(id=ADMIN_ID, email="admin@test.com", password_hash="x", role="super_admin"))
    db.add(Tenant(id=T1, name="Tenant One", slug="tenant-one", status="active", plan="pro"))
    db.add(Tenant(id=T2, name="Tenant Two", slug="tenant-two", status="active", plan="pro"))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(fiscal_routes.router)
    test_app.include_router(fiscal_routes.api_router)
    test_app.include_router(fiscal_routes.payments_router)
    test_app.include_router(fiscal_routes.api_payments_router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, current)
    return TestClient(test_app), db


def test_financial_summary_returns_aggregates():
    client, db = build()
    # seed provisions
    cfg_svc.upsert_fiscal_config(db, T1, {"commission_tax_percent": 10, "walker_tax_percent": 5})
    prov_svc.compute_and_store_provision(db, T1, FakePayment("pay-a", 100, 20, 80), "walk_commission")
    prov_svc.compute_and_store_provision(db, T1, FakePayment("pay-b", 100, 20, 80), "walk_commission")

    r = client.get(f"/admin/tenants/{T1}/financial-summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "platform_tax_reserved" in body and "walker_net" in body
    assert body["count"] == 2
    assert round(body["platform_tax_reserved"], 2) == 4.0
    assert round(body["walker_net"], 2) == 152.0


def test_financial_summary_empty_when_no_provisions():
    client, _ = build()
    r = client.get(f"/admin/tenants/{T2}/financial-summary")
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 0 and r.json()["platform_net"] == 0


def test_financial_summary_404_unknown_tenant():
    client, _ = build()
    r = client.get("/admin/tenants/nao-existe/financial-summary")
    assert r.status_code == 404


def test_get_payment_provision_happy_path():
    client, db = build()
    prov_svc.compute_and_store_provision(db, T1, FakePayment("pay-x", 100, 20, 80), "walk_commission")
    r = client.get("/admin/payments/pay-x/provision")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["payment_id"] == "pay-x" and body["tenant_id"] == T1


def test_get_payment_provision_404_not_found():
    client, _ = build()
    r = client.get("/admin/payments/nao-existe/provision")
    assert r.status_code == 404
