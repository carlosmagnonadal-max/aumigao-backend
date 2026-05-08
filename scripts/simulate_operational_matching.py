import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models import *  # noqa: F403
from app.models.pet import Pet
from app.models.user import User
from app.models.walk import Walk, WalkMatchingAttempt
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


def seed(db, walkers=3, max_attempts=3):
    tutor = User(id="tutor-1", email="tutor@example.com", password_hash="x", full_name="Tutor", role="tutor")
    db.add(tutor)
    db.add(Pet(id="pet-1", tutor_id=tutor.id, name="Mariana", breed="SRD"))
    walker_users = []
    for index in range(1, walkers + 1):
        user = User(id=f"walker-{index}", email=f"walker{index}@example.com", password_hash="x", full_name=f"Walker {index}", role="walker")
        db.add(user)
        db.add(WalkerProfile(id=f"profile-{index}", user_id=user.id, full_name=user.full_name, city="Salvador", state="Pituba", status="approved"))
        walker_users.append(user)
    walk = Walk(
        id=f"walk-{datetime.now(UTC).timestamp()}",
        tutor_id=tutor.id,
        walker_id=walker_users[0].id,
        assigned_walker_id=walker_users[0].id,
        pet_id="pet-1",
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
    return walker_users, walk


def fresh_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)()


def assert_accept_with_privacy():
    db = fresh_session()
    walkers, walk = seed(db)
    start_matching(walk, db)
    db.commit()
    assert serialize_operational_walk(walk, db, user=walkers[0])["address_snapshot"] == ""
    accept_walk(walk, walkers[0], db)
    db.commit()
    payload = serialize_operational_walk(walk, db, user=walkers[0])
    assert payload["operational_status"] == WALKER_ACCEPTED
    assert payload["pickup_privacy_level"] == "full"
    assert "Rua Premium" in payload["address_snapshot"]
    assert db.query(WalkMatchingAttempt).filter_by(status=ACCEPTED_ATTEMPT).count() == 1


def assert_decline_rematches():
    db = fresh_session()
    walkers, walk = seed(db)
    start_matching(walk, db)
    db.commit()
    decline_walk(walk, walkers[0], db)
    db.commit()
    statuses = [item.status for item in db.query(WalkMatchingAttempt).order_by(WalkMatchingAttempt.attempt_number).all()]
    assert statuses == [DECLINED_ATTEMPT, PENDING_ATTEMPT]
    assert walk.operational_status == AUTO_REMATCHING


def assert_expiration_rematches():
    db = fresh_session()
    walkers, walk = seed(db)
    start_matching(walk, db)
    attempt = db.query(WalkMatchingAttempt).first()
    attempt.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    walk.confirmation_expires_at = attempt.expires_at
    db.commit()
    assert process_expired_attempts(db) == 1
    statuses = [item.status for item in db.query(WalkMatchingAttempt).order_by(WalkMatchingAttempt.attempt_number).all()]
    assert statuses == [EXPIRED_ATTEMPT, PENDING_ATTEMPT]


def assert_no_walker_found_after_three():
    db = fresh_session()
    walkers, walk = seed(db, walkers=3, max_attempts=3)
    start_matching(walk, db)
    db.commit()
    for walker in walkers:
        decline_walk(walk, walker, db)
        db.commit()
        db.refresh(walk)
    assert walk.operational_status == NO_WALKER_FOUND
    assert db.query(WalkMatchingAttempt).count() == 3


def assert_simultaneous_acceptance_guard():
    db = fresh_session()
    walkers, walk = seed(db)
    start_matching(walk, db)
    db.commit()
    try:
        accept_walk(walk, walkers[1], db)
        raise AssertionError("unassigned walker accepted")
    except HTTPException:
        pass
    accept_walk(walk, walkers[0], db)
    db.commit()
    try:
        accept_walk(walk, walkers[1], db)
        raise AssertionError("second walker accepted")
    except HTTPException:
        pass


if __name__ == "__main__":
    assert_accept_with_privacy()
    assert_decline_rematches()
    assert_expiration_rematches()
    assert_no_walker_found_after_three()
    assert_simultaneous_acceptance_guard()
    print("operational matching simulation ok")
