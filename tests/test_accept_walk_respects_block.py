"""Task 5 — TDD: accept_walk deve recusar (HTTPException 409) quando o
passeador tem um WalkerAvailabilityException kind='block' cobrindo a
data/horário do passeio.

Casos:
  A. Sem block → accept_walk sucede (sanity check do cenário).
  B. Com block (dia inteiro) cobrindo a data/hora → HTTPException 409.
  C. Com block de faixa cobrindo a hora → HTTPException 409.
  D. Com block de faixa FORA da hora → accept_walk sucede (sem falso positivo).
"""
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models import *  # noqa: F403
from app.models.pet import Pet
from app.models.tenant import Tenant
from app.models.tenant_walker_access import TenantWalkerAccess
from app.models.user import User
from app.models.walk import Walk, WalkMatchingAttempt
from app.models.walker_availability_exception import WalkerAvailabilityException
from app.models.walker_profile import WalkerProfile
from app.services.operational_matching_service import (
    ACCEPTED_ATTEMPT,
    PENDING_ATTEMPT,
    accept_walk,
    start_matching,
)

# Data futura fixa: 2099-07-10 (quinta) às 10:00
_WALK_DATE = date(2099, 7, 10)
_WALK_HOUR = "10:00"
_SCHEDULED_DATE = f"{_WALK_DATE}T{_WALK_HOUR}:00"


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    yield session
    session.close()


def _make_walker(db, walker_id: str = "walker-block-test") -> User:
    user = User(
        id=walker_id,
        email=f"{walker_id}@example.com",
        password_hash="x",
        full_name="Walker Teste Block",
        role="walker",
        is_active=True,
    )
    profile = WalkerProfile(
        id=f"profile-{walker_id}",
        user_id=user.id,
        full_name="Walker Teste Block",
        city="Salvador",
        state="BA",
        status="active",
        active_as_walker=True,
    )
    db.add_all([user, profile])
    return user


def _seed(db) -> dict:
    """Monta cenário mínimo válido para accept_walk funcionar."""
    tenant = Tenant(id="tenant-block", name="Tenant Block", slug="tenant-block", status="active")
    tutor = User(
        id="tutor-block",
        email="tutor-block@example.com",
        password_hash="x",
        full_name="Tutor Block",
        role="tutor",
        tenant_id=tenant.id,
    )
    pet = Pet(id="pet-block", tutor_id=tutor.id, tenant_id=tenant.id, name="Rex", breed="SRD")
    walker = _make_walker(db)
    access = TenantWalkerAccess(
        id="access-block",
        tenant_id=tenant.id,
        walker_user_id=walker.id,
        access_type="shared_network",
        status="active",
    )
    db.add_all([tenant, tutor, pet, access])
    db.commit()
    return {"tenant": tenant, "tutor": tutor, "pet": pet, "walker": walker}


def _make_walk(db, seed) -> Walk:
    walk = Walk(
        id="walk-block-test",
        tutor_id=seed["tutor"].id,
        tenant_id=seed["tenant"].id,
        pet_id=seed["pet"].id,
        scheduled_date=_SCHEDULED_DATE,
        duration_minutes=45,
        price=49.9,
        status="Agendado",
        address_snapshot="Rua Teste, 1 - Salvador",
        walker_selection_mode="auto",
        max_attempts=3,
    )
    db.add(walk)
    db.commit()
    return walk


def _run_matching_and_force_attempt(db, walk, walker) -> WalkMatchingAttempt:
    """Cria um pending attempt apontando para o walker (via start_matching + force)."""
    start_matching(walk, db)
    db.commit()
    # start_matching atribui automaticamente ao único walker elegível do pool.
    attempt = (
        db.query(WalkMatchingAttempt)
        .filter(
            WalkMatchingAttempt.walk_id == walk.id,
            WalkMatchingAttempt.status == PENDING_ATTEMPT,
        )
        .first()
    )
    if attempt is None:
        # Fallback: criar attempt manual apontando para o walker
        attempt = WalkMatchingAttempt(
            id="attempt-block",
            walk_id=walk.id,
            walker_id=walker.id,
            attempt_number=1,
            status=PENDING_ATTEMPT,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )
        db.add(attempt)
        # Atualizar walk para o estado esperado por accept_walk
        walk.operational_status = "pending_walker_confirmation"
        walk.assigned_walker_id = walker.id
        db.commit()
    elif attempt.walker_id != walker.id:
        # Attempt foi para outro walker — force-reaponta
        attempt.walker_id = walker.id
        walk.assigned_walker_id = walker.id
        db.commit()
    return attempt


# ---------------------------------------------------------------------------
# Caso A: sem block → accept_walk sucede (sanity)
# ---------------------------------------------------------------------------

def test_accept_walk_without_block_succeeds(db):
    """Sanity: sem exception block, accept_walk deve completar sem erro."""
    seed = _seed(db)
    walk = _make_walk(db, seed)
    _run_matching_and_force_attempt(db, walk, seed["walker"])

    # Não deve levantar
    result = accept_walk(walk, seed["walker"], db)
    assert result is not None


# ---------------------------------------------------------------------------
# Caso B: block dia inteiro na data do walk → 409
# ---------------------------------------------------------------------------

def test_accept_walk_blocked_full_day_raises_409(db):
    """Block sem faixa (dia inteiro) na data do passeio → HTTPException 409."""
    seed = _seed(db)
    walk = _make_walk(db, seed)
    _run_matching_and_force_attempt(db, walk, seed["walker"])

    block = WalkerAvailabilityException(
        id="block-full-day",
        walker_user_id=seed["walker"].id,
        exception_date=_WALK_DATE,
        kind="block",
        start_time=None,
        end_time=None,
    )
    db.add(block)
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        accept_walk(walk, seed["walker"], db)
    assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# Caso C: block com faixa cobrindo a hora do walk → 409
# ---------------------------------------------------------------------------

def test_accept_walk_blocked_time_range_raises_409(db):
    """Block com faixa [09:00, 12:00) cobre 10:00 → HTTPException 409."""
    seed = _seed(db)
    walk = _make_walk(db, seed)
    _run_matching_and_force_attempt(db, walk, seed["walker"])

    block = WalkerAvailabilityException(
        id="block-range",
        walker_user_id=seed["walker"].id,
        exception_date=_WALK_DATE,
        kind="block",
        start_time="09:00",
        end_time="12:00",
    )
    db.add(block)
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        accept_walk(walk, seed["walker"], db)
    assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# Caso D: block com faixa FORA da hora do walk → sem 409 (sem falso positivo)
# ---------------------------------------------------------------------------

def test_accept_walk_block_outside_range_succeeds(db):
    """Block com faixa [14:00, 18:00) NÃO cobre 10:00 → accept_walk sucede."""
    seed = _seed(db)
    walk = _make_walk(db, seed)
    _run_matching_and_force_attempt(db, walk, seed["walker"])

    block = WalkerAvailabilityException(
        id="block-outside",
        walker_user_id=seed["walker"].id,
        exception_date=_WALK_DATE,
        kind="block",
        start_time="14:00",
        end_time="18:00",
    )
    db.add(block)
    db.commit()

    # Não deve levantar 409
    result = accept_walk(walk, seed["walker"], db)
    assert result is not None
