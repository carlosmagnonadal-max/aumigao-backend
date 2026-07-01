from __future__ import annotations

import app.models  # noqa: F401

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.tenant import Tenant
from app.models.user import User
from app.models.notification import Notification
from app.models.tutor_referral import TutorReferral
from app.services.tutor_referral_notify import notify_tutor_referral_rewards


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan="business"))
    db.add(User(id="u1", email="u1@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.add(User(id="u2", email="u2@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.commit()
    return db


def _ref(db, reward_type="passeio_gratis"):
    ref = TutorReferral(id="r1", tenant_id="t1", referrer_user_id="u1", referred_user_id="u2",
                        referral_code="TUT-A-1", status="converted", reward_status="granted",
                        reward_snapshot_json=json.dumps({"reward_type": reward_type}))
    db.add(ref); db.commit()
    return ref


def test_notifies_both_sides_with_reward_eligible_type():
    db = _db()
    ref = _ref(db)
    notify_tutor_referral_rewards(db, ref)
    notes = db.query(Notification).all()
    assert len(notes) == 2
    assert {n.user_id for n in notes} == {"u1", "u2"}
    assert all(n.type == "reward_eligible" for n in notes)
    assert all(n.tenant_id == "t1" and n.user_role == "tutor" for n in notes)
    assert all(n.related_entity_type == "tutor_referral" and n.related_entity_id == "r1" for n in notes)


def test_message_reflects_reward_type():
    db = _db()
    ref = _ref(db, reward_type="credito")
    notify_tutor_referral_rewards(db, ref)
    msgs = " ".join(n.message.lower() for n in db.query(Notification).all())
    assert "crédito" in msgs or "credito" in msgs
