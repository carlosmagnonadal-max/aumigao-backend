from __future__ import annotations

import app.models  # noqa: F401

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.tenant import Tenant
from app.models.user import User
from app.models.tutor_referral import TutorReferral
from app.services import tutor_referrals as svc


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan="business"))
    db.add(User(id="u1", email="u1@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.add(User(id="u2", email="u2@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.commit()
    return db


def test_create_generates_code_and_link():
    db = _db()
    u1 = db.get(User, "u1")
    ref = svc.create_tutor_referral(db, u1, "t1")
    assert ref.referral_code.startswith("TUT-")
    assert ref.invite_link.endswith(ref.referral_code)
    assert ref.status == "pending"


def test_create_is_idempotent_per_referrer_tenant():
    db = _db()
    u1 = db.get(User, "u1")
    a = svc.create_tutor_referral(db, u1, "t1")
    b = svc.create_tutor_referral(db, u1, "t1")
    assert a.id == b.id


def test_validate_code_ok_and_missing():
    db = _db()
    u1 = db.get(User, "u1")
    ref = svc.create_tutor_referral(db, u1, "t1")
    data = svc.validate_tutor_referral_code(db, ref.referral_code)
    assert data["tenant_id"] == "t1"
    with pytest.raises(HTTPException):
        svc.validate_tutor_referral_code(db, "TUT-NOPE-000")


def test_link_sets_referred_and_registered():
    db = _db()
    u1 = db.get(User, "u1")
    ref = svc.create_tutor_referral(db, u1, "t1")
    linked = svc.link_tutor_referral(db, ref.referral_code, "u2", "t1")
    assert linked.referred_user_id == "u2"
    assert linked.status == "registered"


def test_link_rejects_self_referral():
    db = _db()
    u1 = db.get(User, "u1")
    ref = svc.create_tutor_referral(db, u1, "t1")
    with pytest.raises(HTTPException):
        svc.link_tutor_referral(db, ref.referral_code, "u1", "t1")
