from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models import *  # noqa: F403
from app.models.pet import Pet
from app.models.tenant import Tenant
from app.models.tenant_walker_access import TenantWalkerAccess
from app.models.user import User
from app.models.walk import Walk, WalkMatchingAttempt
from app.models.walker_profile import WalkerProfile
from app.services.operational_matching_service import (
    ACCEPTED_ATTEMPT,
    PENDING_ATTEMPT,
    accept_walk,
    start_matching,
)
from app.services.walker_network_matching_service import (
    get_matching_pool_for_tenant,
    is_walker_eligible_for_tenant,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    yield session
    session.close()


def _scheduled_date() -> str:
    return (datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")


def _walker(db, walker_id: str, name: str) -> User:
    user = User(
        id=walker_id,
        email=f"{walker_id}@example.com",
        password_hash="x",
        full_name=name,
        role="walker",
        is_active=True,
    )
    profile = WalkerProfile(
        id=f"profile-{walker_id}",
        user_id=user.id,
        full_name=name,
        city="Salvador",
        state="Pituba",
        status="active",
        active_as_walker=True,
    )
    db.add_all([user, profile])
    return user


@pytest.fixture()
def tenant_matching_seed(db):
    tenant_a = Tenant(id="tenant-a", name="Tenant A", slug="tenant-a", status="active")
    tenant_b = Tenant(id="tenant-b", name="Tenant B", slug="tenant-b", status="active")
    tutor = User(
        id="tutor-a",
        email="tutor-a@example.com",
        password_hash="x",
        full_name="Tutor A",
        role="tutor",
        tenant_id=tenant_a.id,
    )
    pet = Pet(id="pet-a", tutor_id=tutor.id, tenant_id=tenant_a.id, name="Pet A", breed="SRD")

    walker_elegivel = _walker(db, "walker-elegivel", "Walker Elegivel")
    walker_sem_acesso = _walker(db, "walker-sem-acesso", "Walker Sem Acesso")
    walker_revogado = _walker(db, "walker-revogado", "Walker Revogado")
    walker_outro_tenant = _walker(db, "walker-outro-tenant", "Walker Outro Tenant")

    db.add_all(
        [
            tenant_a,
            tenant_b,
            tutor,
            pet,
            TenantWalkerAccess(
                id="access-elegivel",
                tenant_id=tenant_a.id,
                walker_user_id=walker_elegivel.id,
                access_type="shared_network",
                status="active",
            ),
            TenantWalkerAccess(
                id="access-revogado",
                tenant_id=tenant_a.id,
                walker_user_id=walker_revogado.id,
                access_type="shared_network",
                status="revoked",
            ),
            TenantWalkerAccess(
                id="access-outro-tenant",
                tenant_id=tenant_b.id,
                walker_user_id=walker_outro_tenant.id,
                access_type="shared_network",
                status="active",
            ),
        ]
    )
    db.commit()

    return {
        "tenant_a": tenant_a,
        "tenant_b": tenant_b,
        "tutor": tutor,
        "pet": pet,
        "walker_elegivel": walker_elegivel,
        "walker_sem_acesso": walker_sem_acesso,
        "walker_revogado": walker_revogado,
        "walker_outro_tenant": walker_outro_tenant,
    }


def _walk(db, seed, walk_id: str, *, tenant_id: str | None, walker: User | None = None, mode: str = "auto") -> Walk:
    walk = Walk(
        id=walk_id,
        tutor_id=seed["tutor"].id,
        tenant_id=tenant_id,
        walker_id=walker.id if walker else None,
        assigned_walker_id=walker.id if walker else None,
        pet_id=seed["pet"].id,
        scheduled_date=_scheduled_date(),
        duration_minutes=45,
        price=49.9,
        status="Agendado",
        address_snapshot="Rua Premium, 123 - Pituba - Salvador",
        walker_selection_mode=mode,
        max_attempts=3,
    )
    db.add(walk)
    db.commit()
    return walk


def _pending_attempts(db, walk: Walk) -> list[WalkMatchingAttempt]:
    return (
        db.query(WalkMatchingAttempt)
        .filter(WalkMatchingAttempt.walk_id == walk.id, WalkMatchingAttempt.status == PENDING_ATTEMPT)
        .order_by(WalkMatchingAttempt.attempt_number.asc())
        .all()
    )


def test_eligible_walker_receives_and_accepts_tenant_walk(db, tenant_matching_seed):
    seed = tenant_matching_seed

    assert get_matching_pool_for_tenant(db, seed["tenant_a"].id) == [seed["walker_elegivel"].id]
    assert is_walker_eligible_for_tenant(db, seed["tenant_a"].id, seed["walker_elegivel"].id)

    walk = _walk(db, seed, "walk-a", tenant_id=seed["tenant_a"].id)
    start_matching(walk, db)
    db.commit()

    attempts = _pending_attempts(db, walk)
    assert [attempt.walker_id for attempt in attempts] == [seed["walker_elegivel"].id]

    accept_walk(walk, seed["walker_elegivel"], db)
    db.commit()

    accepted = db.query(WalkMatchingAttempt).filter_by(walk_id=walk.id, status=ACCEPTED_ATTEMPT).one()
    assert accepted.walker_id == seed["walker_elegivel"].id


def test_walker_without_tenant_access_does_not_receive_or_accept_tenant_walk(db, tenant_matching_seed):
    seed = tenant_matching_seed
    walk = _walk(db, seed, "walk-sem-acesso", tenant_id=seed["tenant_a"].id, walker=seed["walker_sem_acesso"])

    start_matching(walk, db)
    db.commit()

    assert [attempt.walker_id for attempt in _pending_attempts(db, walk)] != [seed["walker_sem_acesso"].id]
    with pytest.raises(HTTPException):
        accept_walk(walk, seed["walker_sem_acesso"], db)


def test_revoked_walker_does_not_receive_or_accept_tenant_walk(db, tenant_matching_seed):
    seed = tenant_matching_seed
    walk = _walk(db, seed, "walk-revogado", tenant_id=seed["tenant_a"].id, walker=seed["walker_revogado"])

    start_matching(walk, db)
    db.commit()

    assert [attempt.walker_id for attempt in _pending_attempts(db, walk)] != [seed["walker_revogado"].id]
    with pytest.raises(HTTPException):
        accept_walk(walk, seed["walker_revogado"], db)


def test_other_tenant_walker_does_not_receive_tenant_walk(db, tenant_matching_seed):
    seed = tenant_matching_seed
    walk = _walk(db, seed, "walk-outro-tenant", tenant_id=seed["tenant_a"].id, walker=seed["walker_outro_tenant"])

    start_matching(walk, db)
    db.commit()

    assert [attempt.walker_id for attempt in _pending_attempts(db, walk)] != [seed["walker_outro_tenant"].id]


def test_only_selected_inelegible_walker_does_not_create_attempt(db, tenant_matching_seed):
    seed = tenant_matching_seed
    walk = _walk(
        db,
        seed,
        "walk-only-selected",
        tenant_id=seed["tenant_a"].id,
        walker=seed["walker_sem_acesso"],
        mode="only_selected",
    )

    start_matching(walk, db)
    db.commit()

    assert _pending_attempts(db, walk) == []


def test_accept_blocks_walker_revoked_after_attempt_creation(db, tenant_matching_seed):
    seed = tenant_matching_seed
    walk = _walk(db, seed, "walk-revoked-after-attempt", tenant_id=seed["tenant_a"].id)
    start_matching(walk, db)
    db.commit()

    access = db.query(TenantWalkerAccess).filter_by(id="access-elegivel").one()
    access.status = "revoked"
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        accept_walk(walk, seed["walker_elegivel"], db)

    assert exc_info.value.status_code == 403


def test_legacy_walk_without_tenant_keeps_global_matching(db, tenant_matching_seed):
    seed = tenant_matching_seed
    walk = _walk(db, seed, "walk-legacy", tenant_id=None)

    start_matching(walk, db)
    db.commit()

    attempts = _pending_attempts(db, walk)
    assert len(attempts) == 1
    assert attempts[0].walker_id in {
        seed["walker_elegivel"].id,
        seed["walker_sem_acesso"].id,
        seed["walker_revogado"].id,
        seed["walker_outro_tenant"].id,
    }
