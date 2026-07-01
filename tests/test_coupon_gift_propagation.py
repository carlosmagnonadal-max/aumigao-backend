from __future__ import annotations

import app.models  # noqa: F401

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.tenant import Tenant, TenantFeature
from app.models.coupon import Coupon
from app.models.walk import Walk
from app.services import coupon_service as cs


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan="business"))
    # coupons is NOT default-ON — must add a TenantFeature row to enable it.
    db.add(TenantFeature(tenant_id="t1", feature_key="coupons", enabled=True))
    db.add(Walk(id="w1", tenant_id="t1", tutor_id="u2", price=50.0, operational_status="agendado",
                pet_id="p1", scheduled_date="2026-07-01", duration_minutes=30))
    db.add(Coupon(id="c1", tenant_id="t1", code="TREF-R1-RED", discount_type="percent",
                  discount_value=100.0, max_uses=1, max_uses_per_user=1, active=True,
                  is_referral_gift=True))
    db.commit()
    return db


def test_redeeming_gift_coupon_flags_walk():
    db = _db()
    tenant = db.get(Tenant, "t1")
    cs.redeem(db, tenant, "TREF-R1-RED", user_id="u2", amount=50.0, walk_id="w1")
    walk = db.get(Walk, "w1")
    assert walk.is_referral_gift is True
