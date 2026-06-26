"""Testes para rota GET /admin/tenants/{id}/provisions.

Reusa o scaffolding de tests/test_routes_financial_summary.py.
"""
import pytest
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
        self.id = id
        self.amount = amount
        self.platform_amount = platform_amount
        self.walker_amount = walker_amount


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


def test_list_provisions_route_returns_items():
    client, db = build()
    cfg_svc.upsert_fiscal_config(db, T1, {"commission_tax_percent": 10, "walker_tax_percent": 5})
    prov_svc.compute_and_store_provision(db, T1, FakePayment("pay-a", 100, 20, 80), "walk_commission")
    prov_svc.compute_and_store_provision(db, T1, FakePayment("pay-b", 100, 20, 80), "walk_commission")

    r = client.get(f"/admin/tenants/{T1}/provisions?limit=10&offset=0")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body
    assert len(body["items"]) == 2
    item = body["items"][0]
    assert set(["payment_id", "revenue_type", "gross", "tax", "net"]).issubset(item.keys())


def test_list_provisions_route_pagination():
    client, db = build()
    cfg_svc.upsert_fiscal_config(db, T1, {"commission_tax_percent": 10, "walker_tax_percent": 5})
    for i in range(3):
        prov_svc.compute_and_store_provision(db, T1, FakePayment(f"pay-{i}", 100, 20, 80), "walk_commission")

    r = client.get(f"/admin/tenants/{T1}/provisions?limit=2&offset=0")
    assert r.status_code == 200
    assert len(r.json()["items"]) == 2

    r2 = client.get(f"/admin/tenants/{T1}/provisions?limit=2&offset=2")
    assert r2.status_code == 200
    assert len(r2.json()["items"]) == 1


def test_list_provisions_route_404_unknown_tenant():
    client, _ = build()
    r = client.get("/admin/tenants/nao-existe/provisions?limit=10&offset=0")
    assert r.status_code == 404


def test_list_provisions_route_via_api_prefix():
    client, db = build()
    cfg_svc.upsert_fiscal_config(db, T1, {"commission_tax_percent": 10})
    prov_svc.compute_and_store_provision(db, T1, FakePayment("pay-x", 100, 20, 80), "walk_commission")

    r = client.get(f"/api/admin/tenants/{T1}/provisions?limit=10&offset=0")
    assert r.status_code == 200
    assert len(r.json()["items"]) == 1
