from __future__ import annotations

import app.models  # noqa: F401

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.coupon import Coupon
from app.models.walk import Walk


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def test_coupon_gift_flag_defaults_false():
    db = _db()
    c = Coupon(tenant_id="t1", code="X", discount_type="percent", discount_value=10.0)
    db.add(c); db.commit(); db.refresh(c)
    assert c.is_referral_gift is False


def test_walk_gift_flag_defaults_false():
    db = _db()
    w = Walk(
        id="w1",
        tenant_id="t1",
        tutor_id="u1",
        pet_id="pet1",
        scheduled_date="2026-07-01",
        duration_minutes=30,
        price=50.0,
        operational_status="agendado",
    )
    db.add(w); db.commit(); db.refresh(w)
    assert w.is_referral_gift is False
