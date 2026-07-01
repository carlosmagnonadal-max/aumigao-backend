"""FIX 7 (P1) — período da assinatura SaaS do tenant no webhook de confirmação usa
_period_end_month (overflow-safe) em vez de timedelta(days=31).

O +31 dias desalinha o vencimento (31/jan + 31d = 03/mar, pula fevereiro) e
desloca o ciclo a cada mês. O correto é +1 mês com clamp de fim-de-mês.
"""
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base, get_db, get_global_db
from app.models.tenant import Tenant
from app.models.tenant_saas_subscription import TenantSaasSubscription, SAAS_ACTIVE
from app.services.tenant_saas_billing_service import _period_end_month
from app.routes import payments
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-saas-p"
TOKEN = "wh-saas-p"


def test_period_end_month_clamps_jan31_to_feb():
    # 31/jan + 1 mês -> 28/29 fev (NÃO 03/mar como +31 dias faria).
    end = _period_end_month(datetime(2026, 1, 31, 12, 0))
    assert end.month == 2
    assert end.day in (28, 29)


def test_period_end_month_differs_from_31_days_at_month_border():
    from datetime import timedelta
    now = datetime(2026, 1, 31, 12, 0)
    assert _period_end_month(now) != now + timedelta(days=31)


def _build():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="pro"))
    db.add(TenantSaasSubscription(
        id="saas-1", tenant_id=TENANT_ID, plan="pro", price=129.90, status="overdue",
        asaas_subscription_id="asaas-saas-1",
    ))
    db.commit()
    app = FastAPI()
    app.include_router(payments.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_global_db] = lambda: db
    return TestClient(app), db


def test_webhook_confirm_sets_period_end_via_month_helper(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", TOKEN)
    monkeypatch.setattr(payments, "PAYMENT_MODE", "asaas_sandbox")
    client, db = _build()

    r = client.post(
        "/payments/webhooks/asaas",
        headers={"asaas-access-token": TOKEN},
        json={
            "id": "evt_saas_confirm",
            "event": "PAYMENT_CONFIRMED",
            "payment": {
                "id": "asaas-pay-saas",
                "externalReference": "tenant_sub:saas-1",
                "subscription": "asaas-saas-1",
                "value": 129.90,
                "status": "CONFIRMED",
            },
        },
    )
    assert r.status_code == 200, r.text
    db.expire_all()
    sub = db.get(TenantSaasSubscription, "saas-1")
    assert sub.status == SAAS_ACTIVE
    # period_end == _period_end_month(period_start): +1 mês overflow-safe.
    expected = _period_end_month(sub.current_period_start)
    assert sub.current_period_end == expected
