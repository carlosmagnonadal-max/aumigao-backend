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


# ---------------------------------------------------------------------------
# Task 6: refresh_referral_conversion — os 3 gatilhos
# ---------------------------------------------------------------------------

import json
import uuid

from app.models.tutor_referral import TutorReferralConfig
from app.models.walk import Walk


def _enable(db, **cfg):
    c = TutorReferralConfig(tenant_id="t1", enabled=True, **cfg)
    db.add(c); db.commit()
    return c


def _paid_completed_walk(db, tutor_id, status="ride_completed", price=50.0):
    db.add(Walk(
        id=uuid.uuid4().hex,
        tenant_id="t1",
        tutor_id=tutor_id,
        pet_id="pet1",
        scheduled_date="2026-07-01",
        duration_minutes=30,
        price=price,
        operational_status=status,
    ))
    db.commit()


def _registered_referral(db):
    u1 = db.get(User, "u1")
    ref = svc.create_tutor_referral(db, u1, "t1")
    return svc.link_tutor_referral(db, ref.referral_code, "u2", "t1")


def test_no_cadastro_converts_immediately():
    db = _db(); _enable(db, trigger_type="no_cadastro")
    ref = _registered_referral(db)
    svc.refresh_referral_conversion(db, "u2", "t1")
    db.refresh(ref)
    assert ref.status == "converted"
    assert ref.reward_status == "eligible"
    assert json.loads(ref.reward_snapshot_json)["reward_type"] == "desconto"


def test_primeiro_passeio_pago_needs_a_paid_walk():
    db = _db(); _enable(db, trigger_type="primeiro_passeio_pago")
    ref = _registered_referral(db)
    svc.refresh_referral_conversion(db, "u2", "t1")
    db.refresh(ref); assert ref.status == "registered"
    _paid_completed_walk(db, "u2")
    svc.refresh_referral_conversion(db, "u2", "t1")
    db.refresh(ref); assert ref.status == "converted"


def test_n_passeios_threshold():
    db = _db(); _enable(db, trigger_type="n_passeios", trigger_n=2)
    ref = _registered_referral(db)
    _paid_completed_walk(db, "u2")
    svc.refresh_referral_conversion(db, "u2", "t1")
    db.refresh(ref); assert ref.status == "registered"
    _paid_completed_walk(db, "u2")
    svc.refresh_referral_conversion(db, "u2", "t1")
    db.refresh(ref); assert ref.status == "converted"
    assert ref.completed_paid_walks_count == 2


def test_disabled_config_does_not_convert():
    db = _db()
    ref = _registered_referral(db)   # nenhuma config habilitada
    svc.refresh_referral_conversion(db, "u2", "t1")
    db.refresh(ref); assert ref.status == "registered"


def test_conversion_is_idempotent():
    db = _db(); _enable(db, trigger_type="no_cadastro")
    ref = _registered_referral(db)
    svc.refresh_referral_conversion(db, "u2", "t1")
    svc.refresh_referral_conversion(db, "u2", "t1")  # 2ª vez não muda nada
    db.refresh(ref)
    assert ref.status == "converted"


def test_price_zero_walk_does_not_count():
    db = _db(); _enable(db, trigger_type="primeiro_passeio_pago")
    ref = _registered_referral(db)
    _paid_completed_walk(db, "u2", price=0.0)   # passeio grátis
    svc.refresh_referral_conversion(db, "u2", "t1")
    db.refresh(ref); assert ref.status == "registered"   # não converteu


def test_walk_from_other_tenant_does_not_count():
    db = _db(); _enable(db, trigger_type="primeiro_passeio_pago")
    ref = _registered_referral(db)
    # passeio do convidado, mas em outro tenant
    import uuid as _uuid
    db.add(Walk(id=_uuid.uuid4().hex, tenant_id="t2", tutor_id="u2", price=50.0,
                operational_status="ride_completed", pet_id="pet1",
                scheduled_date="2026-07-01", duration_minutes=30))
    db.commit()
    svc.refresh_referral_conversion(db, "u2", "t1")
    db.refresh(ref); assert ref.status == "registered"


# ---------------------------------------------------------------------------
# Task 7: grant_reward gated por TUTOR_REFERRAL_PAYOUT_ENABLED
# ---------------------------------------------------------------------------

from app.models.coupon import Coupon


def test_conversion_grants_when_flag_on(monkeypatch):
    db = _db(); _enable(db, trigger_type="no_cadastro", reward_type="desconto",
                        discount_kind="fixed", discount_value=15.0)
    ref = _registered_referral(db)
    monkeypatch.setenv("TUTOR_REFERRAL_PAYOUT_ENABLED", "true")
    svc.refresh_referral_conversion(db, "u2", "t1")
    db.refresh(ref)
    assert ref.reward_status == "granted"
    assert db.query(Coupon).filter(Coupon.tenant_id == "t1").count() == 2


def test_conversion_does_not_grant_when_flag_off(monkeypatch):
    db = _db(); _enable(db, trigger_type="no_cadastro", reward_type="desconto",
                        discount_kind="fixed", discount_value=15.0)
    ref = _registered_referral(db)
    monkeypatch.setenv("TUTOR_REFERRAL_PAYOUT_ENABLED", "false")
    svc.refresh_referral_conversion(db, "u2", "t1")
    db.refresh(ref)
    assert ref.status == "converted"
    assert ref.reward_status == "eligible"
    assert db.query(Coupon).filter(Coupon.tenant_id == "t1").count() == 0
