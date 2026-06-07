from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models import *  # noqa: F403
from app.models.pet import Pet
from app.models.user import User
from app.models.walk import Walk, WalkMatchingAttempt, WalkOperationalLog
from app.models.walker_profile import WalkerProfile
from app.services.operational_matching_service import (
    ACCEPTED_ATTEMPT,
    AUTO_REMATCHING,
    DECLINED_ATTEMPT,
    EXPIRED_ATTEMPT,
    NO_WALKER_FOUND,
    PENDING_ATTEMPT,
    WALKER_ACCEPTED,
    accept_walk,
    decline_walk,
    process_expired_attempts,
    serialize_operational_walk,
    start_matching,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    yield session
    session.close()


def _seed(db, walkers: int = 3, max_attempts: int = 3):
    tutor = User(id="tutor-1", email="tutor@example.com", password_hash="x", full_name="Tutor", role="tutor")
    db.add(tutor)
    pet = Pet(id="pet-1", tutor_id=tutor.id, name="Mariana", breed="SRD")
    db.add(pet)
    walker_users = []
    for index in range(1, walkers + 1):
        user = User(id=f"walker-{index}", email=f"walker{index}@example.com", password_hash="x", full_name=f"Walker {index}", role="walker")
        profile = WalkerProfile(
            id=f"profile-{index}",
            user_id=user.id,
            full_name=user.full_name,
            city="Salvador",
            state="Pituba",
            status="approved",
        )
        db.add_all([user, profile])
        walker_users.append(user)
    walk = Walk(
        id="walk-1",
        tutor_id=tutor.id,
        walker_id=walker_users[0].id,
        assigned_walker_id=walker_users[0].id,
        pet_id=pet.id,
        scheduled_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S"),
        duration_minutes=45,
        price=49.9,
        status="Agendado",
        address_snapshot="Rua Premium, 123 - Pituba - Salvador",
        notes="Apartamento 301",
        max_attempts=max_attempts,
    )
    db.add(walk)
    db.commit()
    return tutor, walker_users, walk


def test_walker_accepts_within_deadline_and_address_is_released(db):
    _, walkers, walk = _seed(db)
    start_matching(walk, db)
    db.commit()

    before = serialize_operational_walk(walk, db, user=walkers[0])
    assert before["pickup_privacy_level"] == "coarse"
    assert before["address_snapshot"] == ""

    accept_walk(walk, walkers[0], db)
    db.commit()

    after = serialize_operational_walk(walk, db, user=walkers[0])
    assert after["operational_status"] == WALKER_ACCEPTED
    assert after["pickup_privacy_level"] == "full"
    assert "Rua Premium" in after["address_snapshot"]
    assert db.query(WalkMatchingAttempt).filter_by(status=ACCEPTED_ATTEMPT).count() == 1
    assert db.query(WalkOperationalLog).filter_by(event_type="address_released").count() == 1


def test_decline_creates_rematch_attempt(db):
    _, walkers, walk = _seed(db)
    start_matching(walk, db)
    db.commit()

    decline_walk(walk, walkers[0], db)
    db.commit()

    attempts = db.query(WalkMatchingAttempt).order_by(WalkMatchingAttempt.attempt_number.asc()).all()
    assert [item.status for item in attempts] == [DECLINED_ATTEMPT, PENDING_ATTEMPT]
    assert walk.operational_status == AUTO_REMATCHING
    assert walk.assigned_walker_id == walkers[1].id


def test_expiration_runs_without_app_open_and_rematches(db):
    _, walkers, walk = _seed(db)
    start_matching(walk, db)
    attempt = db.query(WalkMatchingAttempt).first()
    attempt.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    walk.confirmation_expires_at = attempt.expires_at
    db.commit()

    processed = process_expired_attempts(db)
    db.refresh(walk)

    attempts = db.query(WalkMatchingAttempt).order_by(WalkMatchingAttempt.attempt_number.asc()).all()
    assert processed == 1
    assert [item.status for item in attempts] == [EXPIRED_ATTEMPT, PENDING_ATTEMPT]
    assert walk.assigned_walker_id == walkers[1].id


def test_max_three_attempts_then_no_walker_found(db):
    _, walkers, walk = _seed(db, walkers=3, max_attempts=3)
    start_matching(walk, db)
    db.commit()
    for walker in walkers:
        decline_walk(walk, walker, db)
        db.commit()
        db.refresh(walk)

    assert walk.operational_status == NO_WALKER_FOUND
    assert db.query(WalkMatchingAttempt).count() == 3


def test_simultaneous_acceptance_does_not_assign_two_walkers(db):
    _, walkers, walk = _seed(db)
    start_matching(walk, db)
    db.commit()

    with pytest.raises(HTTPException):
        accept_walk(walk, walkers[1], db)

    accept_walk(walk, walkers[0], db)
    db.commit()

    with pytest.raises(HTTPException):
        accept_walk(walk, walkers[1], db)
    assert walk.walker_id == walkers[0].id
