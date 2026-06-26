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


# ------------------------------------------------ middleware / Task 7 ---

def test_is_path_allowlisted():
    from app.services.tenant_status_service import is_path_allowlisted
    assert is_path_allowlisted("/admin/x") and is_path_allowlisted("/payments/webhooks/asaas")
    assert is_path_allowlisted("/health") and not is_path_allowlisted("/walks/1")


def test_get_tenant_status():
    from app.services.tenant_status_service import get_tenant_status
    db = _make_db()
    assert get_tenant_status(TENANT_ID, _sessionmaker_for(db)) == "active"
    assert get_tenant_status("inexistente", _sessionmaker_for(db)) is None


def test_middleware_blocks_suspended_tenant():
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.responses import JSONResponse
    from starlette.testclient import TestClient as STC
    from app.middleware.tenant_resolver import TenantResolverMiddleware
    from app.services.tenant_resolver_service import clear_tenant_cache
    clear_tenant_cache()
    db = _make_db(); t = db.get(Tenant, TENANT_ID); t.status = "suspended"; db.commit()
    Session = _sessionmaker_for(db)
    async def dummy(request): return JSONResponse({"ok": True})
    star = Starlette(routes=[Route("/walks/1", dummy), Route("/health", dummy)])
    star.add_middleware(TenantResolverMiddleware, session_factory=Session)
    c = STC(star)
    # resolve_tenant_identity reads X-Tenant-Id header (via resolve_tenant_from_headers)
    hdr = {"X-Tenant-Id": TENANT_ID}
    assert c.get("/walks/1", headers=hdr).status_code == 403
    assert c.get("/health", headers=hdr).status_code == 200
    clear_tenant_cache()


# -------------------------------------------------- sweep / Task 8 ---

def test_sweep_suspends_overdue_over_7d():
    from app.services.tenant_saas_billing_service import sweep_overdue_tenants
    db = _make_db(); t = db.get(Tenant, TENANT_ID)
    db.add(TenantSaasSubscription(tenant_id=t.id, plan="pro", price=129.90, status=SAAS_OVERDUE,
                                  overdue_since=datetime.utcnow()-timedelta(days=8))); db.commit()
    assert sweep_overdue_tenants(db) == 1; db.commit()
    db.refresh(t); assert t.status=="suspended" and t.suspended_reason=="billing"

def test_sweep_skips_recent_and_paused():
    from app.services.tenant_saas_billing_service import sweep_overdue_tenants
    db = _make_db(); t = db.get(Tenant, TENANT_ID); t.status="paused"; db.commit()
    db.add(TenantSaasSubscription(tenant_id=t.id, plan="pro", price=129.90, status=SAAS_OVERDUE,
                                  overdue_since=datetime.utcnow()-timedelta(days=8))); db.commit()
    assert sweep_overdue_tenants(db) == 0
    db.refresh(t); assert t.status=="paused"  # não toca paused


# -------------------------------------------------- admin endpoints / Task 9 ---

def _make_admin_client(db):
    import app.routes.tenants as tenants_mod
    db.add(User(id="sadmin", email="sa@x.com", password_hash="x", role="super_admin")); db.commit()
    app_t = FastAPI(); app_t.include_router(tenants_mod.router)
    app_t.dependency_overrides[get_db] = lambda: db
    app_t.dependency_overrides[get_current_user] = lambda: db.get(User, "sadmin")
    return TestClient(app_t)

def test_admin_start_get_cancel_saas(monkeypatch):
    import app.routes.tenants as tenants_mod
    async def _start(db, tenant, price=None):
        sub = TenantSaasSubscription(tenant_id=tenant.id, plan=tenant.plan, price=129.90, status=SAAS_ACTIVE)
        db.add(sub); db.commit(); db.refresh(sub); return sub
    async def _cancel(db, tenant):
        s = db.query(TenantSaasSubscription).filter(TenantSaasSubscription.tenant_id==tenant.id, TenantSaasSubscription.status==SAAS_ACTIVE).first()
        if s: s.status = SAAS_CANCELLED; db.commit()
        return s
    monkeypatch.setattr(tenants_mod.saas_billing, "start_subscription", _start)
    monkeypatch.setattr(tenants_mod.saas_billing, "cancel_subscription", _cancel)
    db = _make_db(); client = _make_admin_client(db)
    r = client.post(f"/admin/tenants/{TENANT_ID}/saas-subscription", json={})
    assert r.status_code == 200, r.text
    g = client.get(f"/admin/tenants/{TENANT_ID}/saas-subscription")
    assert g.status_code == 200 and g.json().get("status") == "active", g.text
    d = client.delete(f"/admin/tenants/{TENANT_ID}/saas-subscription")
    assert d.status_code == 200, d.text


# -------------------------------------------------- platform summary / Task 10 ---

def _make_platform_admin_client(db):
    import app.routes.admin as admin_mod
    sa = User(id="sadmin2", email="sa2@x.com", password_hash="x", role="super_admin")
    try:
        db.add(sa); db.commit()
    except Exception:
        db.rollback()
    app_t = FastAPI()
    app_t.include_router(admin_mod.router)
    app_t.dependency_overrides[get_db] = lambda: db
    app_t.dependency_overrides[get_current_user] = lambda: db.get(User, "sadmin2")
    return TestClient(app_t)


def test_saas_revenue_separated(monkeypatch):
    db = _make_db()
    # 1 pagamento de mensalidade SaaS + 1 de passeio
    db.add(Payment(id="pay-saas", tenant_id=TENANT_ID, tutor_id=TENANT_ID, walk_id=None,
                   amount=129.90, status="pagamento_confirmado_sandbox", provider="asaas_tenant_saas",
                   provider_payment_id="ps1"))
    db.add(Payment(id="pay-walk", tenant_id=TENANT_ID, tutor_id="tut", walk_id="w1",
                   amount=50.0, status="pagamento_confirmado_sandbox", provider="internal",
                   provider_payment_id="pw1"))
    db.commit()
    client = _make_platform_admin_client(db)
    r = client.get("/admin/platform/summary")
    assert r.status_code == 200, r.text
    body = r.json()
    rev = body["platform_revenue"]
    # saas_revenue deve capturar exatamente os 129.90
    assert abs(float(rev["saas_revenue"]) - 129.90) < 0.01, f"saas_revenue={rev['saas_revenue']}"
    # total_paid_all_time e gross_revenue_plans NÃO devem incluir a mensalidade SaaS
    assert abs(float(rev["total_paid_all_time"]) - 50.0) < 0.01, (
        f"total_paid_all_time={rev['total_paid_all_time']} deve ser 50.0, não incluir SaaS"
    )
    assert abs(float(rev["gross_revenue_plans"]) - 0.0) < 0.01, (
        f"gross_revenue_plans={rev['gross_revenue_plans']} deve ser 0.0, SaaS excluído"
    )


# ----------------------------------------- B1 + M2 / Task 11 ---

def test_start_subscription_cancels_overdue_previous(monkeypatch):
    import asyncio, app.services.tenant_saas_billing_service as svc
    db = _make_db(); t = db.get(Tenant, TENANT_ID)
    old = TenantSaasSubscription(tenant_id=t.id, plan="pro", price=129.90, status=SAAS_OVERDUE, asaas_subscription_id="old_as")
    db.add(old); db.commit(); old_id = old.id
    cancelled = {}
    async def _cancel(asaas_id): cancelled["id"] = asaas_id
    monkeypatch.setattr(svc, "cancel_asaas_subscription", _cancel)
    monkeypatch.setattr(svc, "ensure_tenant_asaas_customer", _acoro("cus_1"))
    monkeypatch.setattr(svc, "create_asaas_subscription_native", _acoro("new_as"))
    sub = asyncio.run(svc.start_subscription(db, t))
    db.refresh(db.get(TenantSaasSubscription, old_id))
    assert db.get(TenantSaasSubscription, old_id).status == SAAS_CANCELLED  # antiga cancelada
    assert cancelled.get("id") == "old_as"  # cancelou no Asaas
    assert sub.status == SAAS_ACTIVE and sub.asaas_subscription_id == "new_as"

def test_start_subscription_rejects_when_asaas_returns_none(monkeypatch):
    import asyncio, app.services.tenant_saas_billing_service as svc
    db = _make_db(); t = db.get(Tenant, TENANT_ID)
    monkeypatch.setattr(svc, "ensure_tenant_asaas_customer", _acoro("cus_1"))
    monkeypatch.setattr(svc, "create_asaas_subscription_native", _acoro(None))  # gateway não configurado
    with pytest.raises(HTTPException):
        asyncio.run(svc.start_subscription(db, t))
    assert db.query(TenantSaasSubscription).filter(TenantSaasSubscription.status==SAAS_ACTIVE).count() == 0
