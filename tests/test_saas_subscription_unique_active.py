"""FIX 10 (P2) — <=1 assinatura SaaS ativa/inadimplente por tenant (índice único
parcial). Defesa de banco além do código, contra cobrança dupla do tenant.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base
from app.models.tenant import Tenant
from app.models.tenant_saas_subscription import TenantSaasSubscription
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-uq"


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="pro"))
    db.commit()
    return db


def _sub(tid, status):
    return TenantSaasSubscription(id=f"s-{status}-{tid}", tenant_id=tid, plan="pro", price=129.9, status=status)


def test_two_active_subscriptions_rejected():
    db = _db()
    db.add(_sub(TENANT_ID, "active")); db.commit()
    db.add(TenantSaasSubscription(id="s-dup", tenant_id=TENANT_ID, plan="pro", price=129.9, status="active"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_active_plus_overdue_rejected():
    db = _db()
    db.add(_sub(TENANT_ID, "active")); db.commit()
    db.add(TenantSaasSubscription(id="s-ov", tenant_id=TENANT_ID, plan="pro", price=129.9, status="overdue"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_cancelled_does_not_count():
    db = _db()
    db.add(TenantSaasSubscription(id="s-c1", tenant_id=TENANT_ID, plan="pro", price=129.9, status="cancelled"))
    db.add(TenantSaasSubscription(id="s-c2", tenant_id=TENANT_ID, plan="pro", price=129.9, status="cancelled"))
    db.commit()  # múltiplas canceladas OK
    db.add(_sub(TENANT_ID, "active"))
    db.commit()  # 1 ativa + N canceladas OK
    assert db.query(TenantSaasSubscription).filter_by(tenant_id=TENANT_ID).count() == 3
