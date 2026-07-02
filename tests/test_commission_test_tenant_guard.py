"""FIX 12 (P2) — tenant de teste (pmg) não acumula comissão live.

accrue_commission_for_walk pula tenants de teste (slug em TEST_TENANT_SLUGS,
default "pmg"). Conservador: só blinda o accrue, não inventa cobrança.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base
from app.models.commission_entry import CommissionEntry
from app.models.tenant import Tenant
from app.models.walk import Walk
from app.services.commission_billing_service import accrue_commission_for_walk


def _db(slug):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id="t1", name="X", slug=slug, status="active", plan="pro"))
    db.commit()
    return db


def _walk(db):
    walk = Walk(id="w1", tenant_id="t1", tutor_id="tut1", walker_id="k1", pet_id="pet",
                scheduled_date="2026-06-15", duration_minutes=30, price=40.0, status="Finalizado")
    db.add(walk); db.commit()
    return walk


_SPLIT = {"platform_amount": 4.0, "commission_percent": 10.0}


def test_test_tenant_pmg_does_not_accrue():
    db = _db("pmg")
    walk = _walk(db)
    out = accrue_commission_for_walk(db, walk, _SPLIT, is_network=False, period="2026-06")
    db.commit()
    assert out is None
    assert db.query(CommissionEntry).count() == 0


def test_real_tenant_still_accrues():
    db = _db("aumigao")
    walk = _walk(db)
    out = accrue_commission_for_walk(db, walk, _SPLIT, is_network=False, period="2026-06")
    db.commit()
    assert out is not None
    assert db.query(CommissionEntry).count() == 1


def test_test_tenant_slugs_configurable(monkeypatch):
    monkeypatch.setenv("TEST_TENANT_SLUGS", "sandbox,demo")
    db = _db("demo")
    walk = _walk(db)
    out = accrue_commission_for_walk(db, walk, _SPLIT, is_network=False, period="2026-06")
    db.commit()
    assert out is None
