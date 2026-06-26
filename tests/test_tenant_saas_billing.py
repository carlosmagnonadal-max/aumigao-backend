import os
from datetime import datetime, timedelta
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models
from app.core.database import Base, get_db, get_global_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.user import User
from app.models.payment import Payment
from app.models.tenant_saas_subscription import (
    TenantSaasSubscription, SAAS_ACTIVE, SAAS_OVERDUE, SAAS_CANCELLED,
)

TENANT_ID = "t-saas"

def _make_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=TENANT_ID, name="Cliente X", slug="cliente-x", status="active", plan="pro",
                  legal_name="Cliente X LTDA", document_number="11222333000181", contact_email="fin@clientex.com"))
    db.commit()
    return db

def _sessionmaker_for(db):
    return sessionmaker(bind=db.bind)

def _acoro(v):
    async def _f(*a, **k):
        return v
    return _f


# ------------------------------------------------------------------ pricing ---
from app.services.tenant_saas_pricing import resolve_saas_price

def test_pro_price_is_fixed():
    assert float(resolve_saas_price("pro", None)) == 129.90

def test_enterprise_default_floor():
    assert float(resolve_saas_price("enterprise", None)) == 1199.90

def test_enterprise_custom_overrides():
    assert float(resolve_saas_price("enterprise", 1500.0)) == 1500.0

def test_pro_ignores_custom():
    assert float(resolve_saas_price("pro", 50.0)) == 129.90

def test_enterprise_zero_price_raises():
    with pytest.raises(ValueError):
        resolve_saas_price("enterprise", 0.0)

def test_enterprise_negative_raises():
    with pytest.raises(ValueError):
        resolve_saas_price("enterprise", -10.0)


# --------------------------------------------------------- customer (Task 4) ---

def test_customer_requires_document_and_email():
    import asyncio
    from app.services.tenant_saas_billing_service import ensure_tenant_asaas_customer
    db = _make_db(); t = db.get(Tenant, TENANT_ID); t.document_number = None; db.commit()
    with pytest.raises(HTTPException) as e:
        asyncio.run(ensure_tenant_asaas_customer(db, t))
    assert e.value.status_code == 400

def test_customer_idempotent_when_already_set():
    import asyncio
    from app.services.tenant_saas_billing_service import ensure_tenant_asaas_customer
    db = _make_db(); t = db.get(Tenant, TENANT_ID); t.asaas_customer_id = "cus_existing"; db.commit()
    assert asyncio.run(ensure_tenant_asaas_customer(db, t)) == "cus_existing"


# ------------------------------------------------ subscription (Task 5) ---

def test_start_subscription_persists(monkeypatch):
    import asyncio, app.services.tenant_saas_billing_service as svc
    db = _make_db(); t = db.get(Tenant, TENANT_ID)
    monkeypatch.setattr(svc, "ensure_tenant_asaas_customer", _acoro("cus_1"))
    monkeypatch.setattr(svc, "create_asaas_subscription_native", _acoro("asaas_sub_1"))
    sub = asyncio.run(svc.start_subscription(db, t))
    assert sub.status == SAAS_ACTIVE and float(sub.price) == 129.90 and sub.asaas_subscription_id == "asaas_sub_1"

def test_start_subscription_anti_zombie(monkeypatch):
    import asyncio, app.services.tenant_saas_billing_service as svc
    db = _make_db(); t = db.get(Tenant, TENANT_ID)
    monkeypatch.setattr(svc, "ensure_tenant_asaas_customer", _acoro("cus_1"))
    async def _boom(*a, **k): raise HTTPException(502, "asaas down")
    monkeypatch.setattr(svc, "create_asaas_subscription_native", _boom)
    with pytest.raises(HTTPException):
        asyncio.run(svc.start_subscription(db, t))
    assert db.query(TenantSaasSubscription).count() == 0  # nada persistido


# --------------------------------------------------------- webhook (Task 6) ---

def _make_payments_client(db):
    import app.routes.payments as payments_module
    app_t = FastAPI()
    app_t.include_router(payments_module.router)
    app_t.dependency_overrides[get_db] = lambda: db
    app_t.dependency_overrides[get_global_db] = lambda: db
    app_t.dependency_overrides[get_current_user] = lambda: db.get(User, TENANT_ID)
    return TestClient(app_t)


def test_tenant_saas_webhook_confirmed_reactivates(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "tok")
    db = _make_db(); t = db.get(Tenant, TENANT_ID); t.status = "suspended"; t.suspended_reason = "billing"
    sub = TenantSaasSubscription(tenant_id=t.id, plan="pro", price=129.90, status=SAAS_OVERDUE,
                                 overdue_since=datetime.utcnow() - timedelta(days=10), asaas_subscription_id="as_1")
    db.add(sub); db.commit()
    client = _make_payments_client(db)
    payload = {"event": "PAYMENT_RECEIVED", "payment": {"id": "p1", "status": "RECEIVED",
               "externalReference": f"tenant_sub:{sub.id}", "subscription": "as_1", "value": 129.90}}
    r = client.post("/payments/webhooks/asaas", json=payload, headers={"asaas-access-token": "tok"})
    assert r.status_code == 200
    db.refresh(sub); db.refresh(t)
    assert sub.status == SAAS_ACTIVE and sub.overdue_since is None
    assert t.status == "active" and t.suspended_reason is None


def test_tenant_saas_webhook_overdue_marks(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "tok")
    db = _make_db()
    sub = TenantSaasSubscription(tenant_id=TENANT_ID, plan="pro", price=129.90, status=SAAS_ACTIVE, asaas_subscription_id="as_2")
    db.add(sub); db.commit()
    client = _make_payments_client(db)
    payload = {"event": "PAYMENT_OVERDUE", "payment": {"id": "p2", "status": "OVERDUE",
               "externalReference": f"tenant_sub:{sub.id}", "subscription": "as_2", "value": 129.90}}
    r = client.post("/payments/webhooks/asaas", json=payload, headers={"asaas-access-token": "tok"})
    assert r.status_code == 200
    db.refresh(sub); assert sub.status == SAAS_OVERDUE and sub.overdue_since is not None


def test_manual_suspension_not_reactivated(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "tok")
    db = _make_db(); t = db.get(Tenant, TENANT_ID); t.status = "suspended"; t.suspended_reason = "manual"
    sub = TenantSaasSubscription(tenant_id=t.id, plan="pro", price=129.90, status=SAAS_OVERDUE, asaas_subscription_id="as_3")
    db.add(sub); db.commit()
    client = _make_payments_client(db)
    payload = {"event": "PAYMENT_RECEIVED", "payment": {"id": "p3", "status": "RECEIVED",
               "externalReference": f"tenant_sub:{sub.id}", "subscription": "as_3", "value": 129.90}}
    client.post("/payments/webhooks/asaas", json=payload, headers={"asaas-access-token": "tok"})
    db.refresh(t); assert t.status == "suspended"  # NÃO reativa suspensão manual
