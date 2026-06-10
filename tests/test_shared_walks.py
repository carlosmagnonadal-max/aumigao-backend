import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.payment import Payment
from app.models.pet import Pet
from app.models.shared_walk import (
    PARTICIPANT_PAID,
    SHARED_CANCELLED,
    SHARED_CONFIRMED,
    SHARED_FORMING,
    SharedWalk,
    SharedWalkParticipant,
    TenantSharedWalkConfig,
)
from app.models.tenant import Tenant, TenantFeature
from app.services import shared_walk_service as svc


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[
        Tenant.__table__, TenantFeature.__table__, TenantSharedWalkConfig.__table__,
        SharedWalk.__table__, SharedWalkParticipant.__table__, Pet.__table__, Payment.__table__,
    ])
    return sessionmaker(bind=engine)()


def _tenant(db, *, with_feature=True) -> Tenant:
    t = Tenant(id="t1", name="Aumigao", slug="aumigao", status="active", plan="business")
    db.add(t)
    if with_feature:
        db.add(TenantFeature(tenant_id=t.id, feature_key="shared_walks", enabled=True))
    db.commit()
    return t


def _pet(db, pet_id, tutor_id, *, social=True) -> Pet:
    p = Pet(id=pet_id, tutor_id=tutor_id, name=pet_id, can_walk_with_other_pets=social)
    db.add(p)
    db.commit()
    return p


def test_create_blocked_without_feature():
    db = _db(); t = _tenant(db, with_feature=False); _pet(db, "p1", "tutorA")
    with pytest.raises(HTTPException) as e:
        svc.create_session(db, t, "tutorA", scheduled_date="2026-07-01T10:00:00", duration_minutes=45, host_pet_ids=["p1"], open_to_pool=False)
    assert e.value.status_code == 403


def test_create_same_tutor_two_pets():
    db = _db(); t = _tenant(db); _pet(db, "p1", "tutorA"); _pet(db, "p2", "tutorA")
    s = svc.create_session(db, t, "tutorA", scheduled_date="2026-07-01T10:00:00", duration_minutes=45, host_pet_ids=["p1", "p2"], open_to_pool=False)
    assert s.status == SHARED_FORMING
    assert len(svc.active_participants(s)) == 2
    assert svc.tutor_count(s) == 1


def test_create_exceeds_same_tutor_max():
    db = _db(); t = _tenant(db)
    cfg = svc.get_or_create_config(db, t.id); cfg.max_pets_same_tutor = 2; db.commit()
    for i in range(3):
        _pet(db, f"p{i}", "tutorA")
    with pytest.raises(HTTPException) as e:
        svc.create_session(db, t, "tutorA", scheduled_date="x", duration_minutes=45, host_pet_ids=["p0", "p1", "p2"], open_to_pool=False)
    assert e.value.status_code == 400


def test_pool_forced_off_when_tenant_disabled():
    db = _db(); t = _tenant(db); _pet(db, "p1", "tutorA")
    s = svc.create_session(db, t, "tutorA", scheduled_date="x", duration_minutes=45, host_pet_ids=["p1"], open_to_pool=True)
    assert s.open_to_pool is False  # config.pool_enabled default False


def test_join_guest_compatible():
    db = _db(); t = _tenant(db); _pet(db, "p1", "tutorA"); _pet(db, "g1", "tutorB", social=True)
    s = svc.create_session(db, t, "tutorA", scheduled_date="x", duration_minutes=45, host_pet_ids=["p1"], open_to_pool=False)
    s = svc.join_session(db, t, s.id, "tutorB", "g1")
    assert svc.tutor_count(s) == 2


def test_join_guest_incompatible_pet():
    db = _db(); t = _tenant(db); _pet(db, "p1", "tutorA"); _pet(db, "g1", "tutorB", social=False)
    s = svc.create_session(db, t, "tutorA", scheduled_date="x", duration_minutes=45, host_pet_ids=["p1"], open_to_pool=False)
    with pytest.raises(HTTPException) as e:
        svc.join_session(db, t, s.id, "tutorB", "g1")
    assert e.value.status_code == 400


def test_join_blocked_when_max_tutors_reached():
    db = _db(); t = _tenant(db)
    cfg = svc.get_or_create_config(db, t.id); cfg.max_tutors = 2; db.commit()
    _pet(db, "p1", "tutorA"); _pet(db, "g1", "tutorB"); _pet(db, "h1", "tutorC")
    s = svc.create_session(db, t, "tutorA", scheduled_date="x", duration_minutes=45, host_pet_ids=["p1"], open_to_pool=False)
    s = svc.join_session(db, t, s.id, "tutorB", "g1")
    with pytest.raises(HTTPException) as e:
        svc.join_session(db, t, s.id, "tutorC", "h1")
    assert e.value.status_code == 409


def test_checkout_creates_payment_and_marks_paid():
    db = _db(); t = _tenant(db); _pet(db, "p1", "tutorA"); _pet(db, "p2", "tutorA")
    s = svc.create_session(db, t, "tutorA", scheduled_date="x", duration_minutes=45, host_pet_ids=["p1", "p2"], open_to_pool=False)
    s = svc.checkout(db, t, s.id, "tutorA")
    assert all(p.status == PARTICIPANT_PAID for p in s.participants)
    assert db.query(Payment).count() == 1
    assert db.query(Payment).first().amount == s.price_per_pet * 2


def test_confirm_requires_all_paid():
    db = _db(); t = _tenant(db); _pet(db, "p1", "tutorA"); _pet(db, "g1", "tutorB")
    s = svc.create_session(db, t, "tutorA", scheduled_date="x", duration_minutes=45, host_pet_ids=["p1"], open_to_pool=False)
    s = svc.join_session(db, t, s.id, "tutorB", "g1")
    svc.checkout(db, t, s.id, "tutorA")  # só host pagou
    with pytest.raises(HTTPException) as e:
        svc.confirm_session(db, t, s.id, "tutorA")
    assert e.value.status_code == 409


def test_confirm_success_when_all_paid():
    db = _db(); t = _tenant(db); _pet(db, "p1", "tutorA"); _pet(db, "g1", "tutorB")
    s = svc.create_session(db, t, "tutorA", scheduled_date="x", duration_minutes=45, host_pet_ids=["p1"], open_to_pool=False)
    svc.join_session(db, t, s.id, "tutorB", "g1")
    svc.checkout(db, t, s.id, "tutorA")
    svc.checkout(db, t, s.id, "tutorB")
    s = svc.confirm_session(db, t, s.id, "tutorA")
    assert s.status == SHARED_CONFIRMED


def test_confirm_by_non_host_forbidden():
    db = _db(); t = _tenant(db); _pet(db, "p1", "tutorA"); _pet(db, "p2", "tutorA")
    s = svc.create_session(db, t, "tutorA", scheduled_date="x", duration_minutes=45, host_pet_ids=["p1", "p2"], open_to_pool=False)
    svc.checkout(db, t, s.id, "tutorA")
    with pytest.raises(HTTPException) as e:
        svc.confirm_session(db, t, s.id, "tutorB")
    assert e.value.status_code == 403


def test_host_cancel_cancels_session():
    db = _db(); t = _tenant(db); _pet(db, "p1", "tutorA"); _pet(db, "p2", "tutorA")
    s = svc.create_session(db, t, "tutorA", scheduled_date="x", duration_minutes=45, host_pet_ids=["p1", "p2"], open_to_pool=False)
    s = svc.cancel_participation(db, t, s.id, "tutorA")
    assert s.status == SHARED_CANCELLED
