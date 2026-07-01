from __future__ import annotations

import app.models  # noqa: F401

from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.services.commission_billing_service import accrue_commission_for_walk


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def test_gift_walk_accrues_no_commission():
    db = _db()
    walk = SimpleNamespace(id="w1", tenant_id="t1", price=50.0, is_referral_gift=True)
    split = {"platform_amount": 5.0, "commission_percent": 10.0}
    result = accrue_commission_for_walk(db, walk, split, is_network=False, period="2026-07")
    assert result is None


def test_normal_walk_still_accrues():
    db = _db()
    walk = SimpleNamespace(id="w2", tenant_id="t1", price=50.0, is_referral_gift=False)
    split = {"platform_amount": 5.0, "commission_percent": 10.0}
    result = accrue_commission_for_walk(db, walk, split, is_network=False, period="2026-07")
    assert result is not None
    assert result.amount == 5.0
