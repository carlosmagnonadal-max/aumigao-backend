"""Plano free: ISENTO de assinatura/mensalidade SaaS.

- start_subscription REJEITA tenant free (400, nunca cria cobrança no Asaas).
- sweep_overdue_tenants NUNCA suspende tenant free por inadimplência (cinto de
  segurança pro caso de downgrade pro→free com sub antiga vencida).
- Pro/enterprise: comportamento intocado.
"""
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base
from app.models.tenant import Tenant
from app.models.tenant_saas_subscription import (
    SAAS_OVERDUE,
    TenantSaasSubscription,
)
from app.services.tenant_saas_billing_service import (
    start_subscription,
    sweep_overdue_tenants,
)


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _tenant(db, tid, plan, **kw) -> Tenant:
    t = Tenant(
        id=tid, name=tid, slug=tid, status="active", plan=plan,
        legal_name=f"{tid} LTDA", document_number="11222333000181",
        contact_email=f"fin@{tid}.com", **kw,
    )
    db.add(t)
    db.commit()
    return t


def _overdue_sub(db, tenant_id, days_overdue=10):
    sub = TenantSaasSubscription(
        tenant_id=tenant_id,
        plan="pro",
        price=129.90,
        status=SAAS_OVERDUE,
        overdue_since=datetime.utcnow() - timedelta(days=days_overdue),
        current_period_start=datetime.utcnow() - timedelta(days=40),
        current_period_end=datetime.utcnow() - timedelta(days=10),
    )
    db.add(sub)
    db.commit()
    return sub


@pytest.mark.anyio
async def test_start_subscription_rejects_free_before_any_network():
    # Rejeição acontece ANTES de resolver preço/tocar gateway → sem mock de rede.
    db = _db()
    t = _tenant(db, "t-free", "free")
    with pytest.raises(HTTPException) as exc:
        await start_subscription(db, t)
    assert exc.value.status_code == 400
    assert "gratuito" in exc.value.detail.lower()
    # Nenhuma assinatura zumbi criada.
    assert db.query(TenantSaasSubscription).count() == 0


def test_sweep_never_suspends_free_tenant():
    db = _db()
    # Cenário: tenant fez downgrade pro→free com sub antiga vencida.
    t = _tenant(db, "t-free", "free")
    _overdue_sub(db, "t-free")
    n = sweep_overdue_tenants(db)
    db.commit()
    assert n == 0
    db.refresh(t)
    assert t.status == "active"
    assert t.suspended_reason is None


def test_sweep_still_suspends_pro_tenant():
    db = _db()
    t = _tenant(db, "t-pro", "pro")
    _overdue_sub(db, "t-pro")
    n = sweep_overdue_tenants(db)
    db.commit()
    assert n == 1
    db.refresh(t)
    assert t.status == "suspended"
    assert t.suspended_reason == "billing"


def test_sweep_respects_grace_for_all_plans():
    db = _db()
    t = _tenant(db, "t-pro2", "pro")
    _overdue_sub(db, "t-pro2", days_overdue=3)  # dentro da carência de 7d
    n = sweep_overdue_tenants(db)
    db.commit()
    assert n == 0
    db.refresh(t)
    assert t.status == "active"
