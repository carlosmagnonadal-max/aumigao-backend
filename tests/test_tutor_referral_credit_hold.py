from __future__ import annotations

import app.models  # noqa: F401

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.tenant import Tenant
from app.models.user import User
from app.models.tutor_referral import TutorReferral
from app.models.recurring_plan import TutorSubscription
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


def _sub(db, tutor_id, credits=0):
    s = TutorSubscription(id=f"s-{tutor_id}", tenant_id="t1", tutor_id=tutor_id, plan_id="p1",
                          status="active", price=100.0, walks_per_cycle=4,
                          credits_remaining=credits, credits_granted=True)
    db.add(s); db.commit()
    return s


def _ref(db):
    ref = TutorReferral(id="r1", tenant_id="t1", referrer_user_id="u1", referred_user_id="u2",
                        referral_code="TUT-A-1", status="converted", reward_status="eligible",
                        reward_snapshot_json=json.dumps({
                            "reward_type": "credito", "credit_walks": 2,
                            "same_reward_both_sides": True,
                            "referrer_multiplier": 1.0, "referred_multiplier": 1.0}))
    db.add(ref); db.commit()
    return ref


def test_credit_granted_to_active_subscription():
    db = _db()
    _sub(db, "u1", credits=0)  # referrer tem assinatura; referred (u2) não
    ref = _ref(db)
    rewards.grant_reward(db, ref)
    s1 = db.get(TutorSubscription, "s-u1")
    assert s1.credits_remaining == 2
    db.refresh(ref)
    held = json.loads(ref.held_credits_json)
    assert held.get("referred") == 2


def test_held_credit_applied_on_subscribe():
    db = _db()
    ref = _ref(db)
    rewards.grant_reward(db, ref)   # ninguém tem assinatura → tudo retido
    db.refresh(ref)
    assert json.loads(ref.held_credits_json)["referrer"] == 2
    s = _sub(db, "u1", credits=4)
    rewards.apply_held_credit_on_subscription(db, s)
    db.refresh(s)
    assert s.credits_remaining == 6
    db.refresh(ref)
    assert json.loads(ref.held_credits_json).get("referrer") in (0, None)


def test_no_double_grant_on_retry():
    db = _db()
    _sub(db, "u1", credits=0)
    ref = _ref(db)
    rewards.grant_reward(db, ref)
    rewards.grant_reward(db, ref)   # 2ª chamada = no-op
    s1 = db.get(TutorSubscription, "s-u1")
    assert s1.credits_remaining == 2   # não 4
