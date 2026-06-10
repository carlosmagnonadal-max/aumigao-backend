"""Testes de unidade do service app/services/walker_referrals.py.

Foco: validate_referral_code, link_referral_to_user, e as transicoes de estado
mark_referral_under_review / mark_referral_approved / mark_referral_rejected.

NAO importa app.main, NAO usa banco real. SQLite em memoria com apenas as tabelas
que o service toca (users, walker_profiles, walker_referrals). Importa app.models
para registrar todos os mappers (User/WalkerProfile possuem relationships para
Pet/Walk/Tenant/TutorProfile) e so cria as tabelas necessarias.
"""
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  — registra todos os mappers no Base.metadata
from app.core.database import Base
from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.models.walker_referral import WalkerReferral
from app.schemas.walker_referral import WalkerReferralCreate
from app.services import walker_referrals as svc


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            User.__table__,
            WalkerProfile.__table__,
            WalkerReferral.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


def _user(db, *, role="cliente", uid=None) -> User:
    uid = uid or str(uuid4())
    user = User(id=uid, email=f"{uid}@test.com", password_hash="x", role=role)
    db.add(user)
    db.commit()
    return user


def _referral(db, *, referrer_id, status="pending", code=None, referred_user_id=None,
              reward_amount=None) -> WalkerReferral:
    code = code or f"AUM-TEST-{uuid4().hex[:6].upper()}"
    referral = WalkerReferral(
        id=str(uuid4()),
        referrer_user_id=referrer_id,
        referred_user_id=referred_user_id,
        referred_name="Fulano",
        referred_phone="11999999999",
        referred_phone_normalized="11999999999",
        city="Sao Paulo",
        neighborhood="Centro",
        referral_code=code,
        invite_link=f"/walker/register?referralCode={code}",
        status=status,
        reward_status="not_eligible",
        performance_status="neutral",
        reward_amount=reward_amount,
    )
    db.add(referral)
    db.commit()
    db.refresh(referral)
    return referral


# --------------------- validate_referral_code ---------------------

def test_validate_referral_code_happy_path():
    db = _db()
    u = _user(db)
    ref = _referral(db, referrer_id=u.id, code="AUM-ABC-123456", status="pending")
    found = svc.validate_referral_code("AUM-ABC-123456", db)
    assert found.id == ref.id


def test_validate_referral_code_strips_whitespace():
    db = _db()
    u = _user(db)
    _referral(db, referrer_id=u.id, code="AUM-ABC-999999", status="pending")
    found = svc.validate_referral_code("  AUM-ABC-999999  ", db)
    assert found.referral_code == "AUM-ABC-999999"


def test_validate_referral_code_not_found_404():
    db = _db()
    with pytest.raises(HTTPException) as exc:
        svc.validate_referral_code("NAO-EXISTE", db)
    assert exc.value.status_code == 404


@pytest.mark.parametrize("status", ["rejected", "cancelled"])
def test_validate_referral_code_unavailable_status_409(status):
    db = _db()
    u = _user(db)
    _referral(db, referrer_id=u.id, code="AUM-X-1", status=status)
    with pytest.raises(HTTPException) as exc:
        svc.validate_referral_code("AUM-X-1", db)
    assert exc.value.status_code == 409
    assert "disponivel" in exc.value.detail


def test_validate_referral_code_already_linked_409():
    db = _db()
    referrer = _user(db)
    referred = _user(db)
    _referral(db, referrer_id=referrer.id, code="AUM-LINK-1", status="registered",
              referred_user_id=referred.id)
    with pytest.raises(HTTPException) as exc:
        svc.validate_referral_code("AUM-LINK-1", db)
    assert exc.value.status_code == 409
    assert "vinculada" in exc.value.detail


# --------------------- link_referral_to_user ---------------------

def test_link_referral_to_user_happy_path():
    db = _db()
    referrer = _user(db)
    referred = _user(db, role="walker")
    _referral(db, referrer_id=referrer.id, code="AUM-LK-1", status="pending")

    linked = svc.link_referral_to_user("AUM-LK-1", referred, db)
    assert linked.referred_user_id == referred.id
    assert linked.status == "registered"
    assert linked.updated_at is not None


def test_link_referral_self_referral_blocked_409():
    db = _db()
    referrer = _user(db)
    _referral(db, referrer_id=referrer.id, code="AUM-SELF-1", status="pending")

    with pytest.raises(HTTPException) as exc:
        svc.link_referral_to_user("AUM-SELF-1", referrer, db)
    assert exc.value.status_code == 409
    assert "propria indicacao" in exc.value.detail


def test_link_referral_invalid_code_propagates_404():
    db = _db()
    u = _user(db)
    with pytest.raises(HTTPException) as exc:
        svc.link_referral_to_user("INEXISTENTE", u, db)
    assert exc.value.status_code == 404


def test_link_referral_already_linked_propagates_409():
    db = _db()
    referrer = _user(db)
    first = _user(db)
    second = _user(db)
    _referral(db, referrer_id=referrer.id, code="AUM-AL-1", status="registered",
              referred_user_id=first.id)
    with pytest.raises(HTTPException) as exc:
        svc.link_referral_to_user("AUM-AL-1", second, db)
    assert exc.value.status_code == 409


# --------------------- mark_referral_under_review ---------------------

@pytest.mark.parametrize("start_status", ["registered", "invited", "pending"])
def test_mark_under_review_transitions(start_status):
    db = _db()
    referrer = _user(db)
    referred = _user(db)
    _referral(db, referrer_id=referrer.id, code=f"AUM-UR-{start_status}",
              status=start_status, referred_user_id=referred.id)

    svc.mark_referral_under_review(referred.id, db)
    ref = db.query(WalkerReferral).filter_by(referred_user_id=referred.id).first()
    assert ref.status == "under_review"


def test_mark_under_review_ignored_when_already_approved():
    db = _db()
    referrer = _user(db)
    referred = _user(db)
    _referral(db, referrer_id=referrer.id, code="AUM-UR-AP", status="approved",
              referred_user_id=referred.id)

    svc.mark_referral_under_review(referred.id, db)
    ref = db.query(WalkerReferral).filter_by(referred_user_id=referred.id).first()
    assert ref.status == "approved"  # nao transiciona


def test_mark_under_review_no_referral_is_noop():
    db = _db()
    # nao deve levantar erro quando nao ha indicacao para o user
    svc.mark_referral_under_review("user-sem-indicacao", db)


# --------------------- mark_referral_approved ---------------------

@pytest.mark.parametrize("start_status", ["registered", "under_review"])
def test_mark_approved_sets_reward_and_amount(start_status):
    db = _db()
    referrer = _user(db)
    referred = _user(db)
    _referral(db, referrer_id=referrer.id, code=f"AUM-AP-{start_status}",
              status=start_status, referred_user_id=referred.id)

    svc.mark_referral_approved(referred.id, db)
    ref = db.query(WalkerReferral).filter_by(referred_user_id=referred.id).first()
    assert ref.status == "approved"
    assert ref.reward_status == "pending"
    assert ref.reward_amount == svc.DEFAULT_REWARD_AMOUNT
    assert ref.approved_at is not None


def test_mark_approved_preserves_existing_reward_amount():
    db = _db()
    referrer = _user(db)
    referred = _user(db)
    _referral(db, referrer_id=referrer.id, code="AUM-AP-KEEP", status="under_review",
              referred_user_id=referred.id, reward_amount=50.0)

    svc.mark_referral_approved(referred.id, db)
    ref = db.query(WalkerReferral).filter_by(referred_user_id=referred.id).first()
    assert ref.reward_amount == 50.0


def test_mark_approved_ignored_from_pending():
    db = _db()
    referrer = _user(db)
    referred = _user(db)
    # "pending" nao esta na lista permitida {registered, under_review}
    _referral(db, referrer_id=referrer.id, code="AUM-AP-PEND", status="pending",
              referred_user_id=referred.id)

    svc.mark_referral_approved(referred.id, db)
    ref = db.query(WalkerReferral).filter_by(referred_user_id=referred.id).first()
    assert ref.status == "pending"
    assert ref.reward_status == "not_eligible"
    assert ref.approved_at is None


# --------------------- mark_referral_rejected ---------------------

@pytest.mark.parametrize("start_status", ["registered", "under_review", "approved"])
def test_mark_rejected_transitions_and_cancels_reward(start_status):
    db = _db()
    referrer = _user(db)
    referred = _user(db)
    _referral(db, referrer_id=referrer.id, code=f"AUM-RJ-{start_status}",
              status=start_status, referred_user_id=referred.id)

    svc.mark_referral_rejected(referred.id, "documentos invalidos", db)
    ref = db.query(WalkerReferral).filter_by(referred_user_id=referred.id).first()
    assert ref.status == "rejected"
    assert ref.reward_status == "cancelled"
    assert ref.rejection_reason == "documentos invalidos"
    assert ref.rejected_at is not None


def test_mark_rejected_accepts_none_reason():
    db = _db()
    referrer = _user(db)
    referred = _user(db)
    _referral(db, referrer_id=referrer.id, code="AUM-RJ-NONE", status="registered",
              referred_user_id=referred.id)

    svc.mark_referral_rejected(referred.id, None, db)
    ref = db.query(WalkerReferral).filter_by(referred_user_id=referred.id).first()
    assert ref.status == "rejected"
    assert ref.rejection_reason is None


def test_mark_rejected_ignored_from_pending():
    db = _db()
    referrer = _user(db)
    referred = _user(db)
    _referral(db, referrer_id=referrer.id, code="AUM-RJ-PEND", status="pending",
              referred_user_id=referred.id)

    svc.mark_referral_rejected(referred.id, "x", db)
    ref = db.query(WalkerReferral).filter_by(referred_user_id=referred.id).first()
    assert ref.status == "pending"


# --------------------- ciclo de estados integrado ---------------------

def test_full_lifecycle_link_review_approve():
    db = _db()
    referrer = _user(db)
    referred = _user(db, role="walker")
    _referral(db, referrer_id=referrer.id, code="AUM-CYC-1", status="pending")

    svc.link_referral_to_user("AUM-CYC-1", referred, db)
    svc.mark_referral_under_review(referred.id, db)
    ref = db.query(WalkerReferral).filter_by(referred_user_id=referred.id).first()
    assert ref.status == "under_review"

    svc.mark_referral_approved(referred.id, db)
    ref = db.query(WalkerReferral).filter_by(referred_user_id=referred.id).first()
    assert ref.status == "approved"
    assert ref.reward_status == "pending"
