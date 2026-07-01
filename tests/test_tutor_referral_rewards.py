from __future__ import annotations

import app.models  # noqa: F401

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.tenant import Tenant
from app.models.user import User
from app.models.coupon import Coupon
from app.models.tutor_referral import TutorReferral
from app.services import tutor_referral_rewards as rewards


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan="business"))
    db.add(User(id="u1", email="u1@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.add(User(id="u2", email="u2@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.commit()
    return db


def _converted_referral(db, snapshot: dict) -> TutorReferral:
    ref = TutorReferral(
        id="r1", tenant_id="t1", referrer_user_id="u1", referred_user_id="u2",
        referral_code="TUT-A-1", status="converted", reward_status="eligible",
        reward_snapshot_json=json.dumps(snapshot),
    )
    db.add(ref); db.commit()
    return ref


def test_desconto_creates_coupon_for_both_sides():
    db = _db()
    ref = _converted_referral(db, {"reward_type": "desconto", "discount_kind": "fixed",
                                   "discount_value": 20.0, "same_reward_both_sides": True,
                                   "referrer_multiplier": 1.0, "referred_multiplier": 1.0})
    rewards.grant_reward(db, ref)
    coupons = db.query(Coupon).filter(Coupon.tenant_id == "t1").all()
    assert len(coupons) == 2
    assert all(c.discount_type == "fixed" and c.discount_value == 20.0 for c in coupons)
    assert all(c.max_uses == 1 and c.is_referral_gift is False for c in coupons)
    db.refresh(ref); assert ref.reward_status == "granted"


def test_passeio_gratis_creates_100pct_gift_coupon():
    db = _db()
    ref = _converted_referral(db, {"reward_type": "passeio_gratis", "free_walks_count": 1,
                                   "same_reward_both_sides": True,
                                   "referrer_multiplier": 1.0, "referred_multiplier": 1.0})
    rewards.grant_reward(db, ref)
    coupons = db.query(Coupon).filter(Coupon.tenant_id == "t1").all()
    assert len(coupons) == 2
    assert all(c.discount_type == "percent" and c.discount_value == 100.0 for c in coupons)
    assert all(c.is_referral_gift is True and c.max_uses == 1 for c in coupons)


def test_grant_is_idempotent():
    db = _db()
    ref = _converted_referral(db, {"reward_type": "desconto", "discount_kind": "fixed",
                                   "discount_value": 20.0, "same_reward_both_sides": True,
                                   "referrer_multiplier": 1.0, "referred_multiplier": 1.0})
    rewards.grant_reward(db, ref)
    rewards.grant_reward(db, ref)
    assert db.query(Coupon).filter(Coupon.tenant_id == "t1").count() == 2
