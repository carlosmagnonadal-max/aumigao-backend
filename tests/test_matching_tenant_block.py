"""TDD — Task 4 + Task 5 (F3.1): matching e accept_walk honram block por tenant.

Task 4 — pool scoring (has_schedule_conflict):
  - Block global (tenant_id=NULL) conflita com qualquer tenant.
  - Block de tenant X só conflita quando o request é de tenant X.

Task 5 — accept_walk recheck:
  - Block do tenant do walk → 409.
  - Block de outro tenant → não bloqueia o aceite.
"""
from datetime import date, datetime, timedelta
from uuid import uuid4

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
from app.schemas.matching import MatchingWalkerRequest
from app.services import matching_service as svc
from app.services.operational_matching_service import (
    PENDING_ATTEMPT,
    accept_walk,
    start_matching,
)


# ---------------------------------------------------------------------------
# Infra de banco — padrão local do repo (sem conftest compartilhado)
# ---------------------------------------------------------------------------

def _db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _block(db, walker_id, d, start, end, tenant_id=None):
    db.add(WalkerAvailabilityException(
        id=str(uuid4()), walker_user_id=walker_id, exception_date=d,
        kind="block", start_time=start, end_time=end, tenant_id=tenant_id,
    ))
    db.commit()


def _req(tenant_id=None):
    return MatchingWalkerRequest(
        scheduled_at="2026-06-22T09:00:00",
        duration_minutes=45,
        tenant_id=tenant_id,
    )


# ---------------------------------------------------------------------------
# Task 4 — has_schedule_conflict filtra por tenant
# ---------------------------------------------------------------------------

def test_block_global_conflita_em_qualquer_tenant():
    db = _db()
    _block(db, "w1", date(2026, 6, 22), "09:00", "10:00", tenant_id=None)
    assert svc.has_schedule_conflict("w1", _req(tenant_id="tA"), db) is True
    assert svc.has_schedule_conflict("w1", _req(tenant_id=None), db) is True


def test_block_de_tenant_so_conflita_naquele_tenant():
    db = _db()
    _block(db, "w1", date(2026, 6, 22), "09:00", "10:00", tenant_id="tA")
    assert svc.has_schedule_conflict("w1", _req(tenant_id="tA"), db) is True
    assert svc.has_schedule_conflict("w1", _req(tenant_id="tB"), db) is False
    assert svc.has_schedule_conflict("w1", _req(tenant_id=None), db) is False


# ---------------------------------------------------------------------------
# Task 5 — accept_walk recheck filtra por tenant
#
# Monta o cenário mínimo válido reusando o padrão de test_accept_walk_respects_block.py.
# ---------------------------------------------------------------------------

# Data fixa no futuro (não colide com datas dos outros testes)
_TASK5_DATE = date(2099, 8, 15)
_TASK5_HOUR = "10:00"
_TASK5_SCHEDULED = f"{_TASK5_DATE}T{_TASK5_HOUR}:00"


def _seed_task5(db, tenant_id_a="tA", tenant_id_b="tB"):
    """Cria dois tenants, um tutor/pet/walker com acesso ativo nos dois tenants."""
    for tid, name, slug in [
        (tenant_id_a, "Tenant A", "tenant-a-t5"),
        (tenant_id_b, "Tenant B", "tenant-b-t5"),
    ]:
        db.add(Tenant(id=tid, name=name, slug=slug, status="active"))
    db.commit()

    tutor = User(
        id="tutor-t5",
        email="tutor-t5@t.invalid",
        password_hash="x",
        full_name="Tutor T5",
        role="tutor",
        tenant_id=tenant_id_a,
    )
    pet = Pet(
        id="pet-t5",
        tutor_id="tutor-t5",
        tenant_id=tenant_id_a,
        name="Bolt",
        breed="SRD",
    )
    walker_user = User(
        id="walker-t5",
        email="walker-t5@t.invalid",
        password_hash="x",
        full_name="Walker T5",
        role="walker",
        is_active=True,
    )
    walker_profile = WalkerProfile(
        id="profile-t5",
        user_id="walker-t5",
        full_name="Walker T5",
        city="Salvador",
        state="BA",
        status="active",
        active_as_walker=True,
    )
    # acesso ativo nos dois tenants
    access_a = TenantWalkerAccess(
        id="access-t5-a",
        tenant_id=tenant_id_a,
        walker_user_id="walker-t5",
        access_type="shared_network",
        status="active",
    )
    access_b = TenantWalkerAccess(
        id="access-t5-b",
        tenant_id=tenant_id_b,
        walker_user_id="walker-t5",
        access_type="shared_network",
        status="active",
    )
    db.add_all([tutor, pet, walker_user, walker_profile, access_a, access_b])
    db.commit()
    return walker_user


def _make_walk_t5(db, tenant_id="tA"):
    walk = Walk(
        id=f"walk-t5-{tenant_id}",
        tutor_id="tutor-t5",
        tenant_id=tenant_id,
        pet_id="pet-t5",
        scheduled_date=_TASK5_SCHEDULED,
        duration_minutes=45,
        price=49.9,
        status="Agendado",
        address_snapshot="Rua Teste T5, 1 - Salvador",
        walker_selection_mode="auto",
        max_attempts=3,
    )
    db.add(walk)
    db.commit()
    return walk


def _force_attempt(db, walk, walker):
    """Garante um pending_attempt apontando para o walker (igual ao padrão existente)."""
    start_matching(walk, db)
    db.commit()
    attempt = (
        db.query(WalkMatchingAttempt)
        .filter(
            WalkMatchingAttempt.walk_id == walk.id,
            WalkMatchingAttempt.status == PENDING_ATTEMPT,
        )
        .first()
    )
    if attempt is None:
        attempt = WalkMatchingAttempt(
            id=f"attempt-t5-{walk.id}",
            walk_id=walk.id,
            walker_id=walker.id,
            attempt_number=1,
            status=PENDING_ATTEMPT,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )
        db.add(attempt)
        walk.operational_status = "pending_walker_confirmation"
        walk.assigned_walker_id = walker.id
        db.commit()
    elif attempt.walker_id != walker.id:
        attempt.walker_id = walker.id
        walk.assigned_walker_id = walker.id
        db.commit()
    return attempt


def test_accept_walk_recusa_block_do_tenant_do_walk():
    """Walk de tA + block tA cobrindo o horário → HTTPException 409."""
    db = _db()
    walker = _seed_task5(db)
    walk = _make_walk_t5(db, tenant_id="tA")
    _force_attempt(db, walk, walker)

    _block(db, walker.id, _TASK5_DATE, "09:00", "12:00", tenant_id="tA")

    with pytest.raises(HTTPException) as exc_info:
        accept_walk(walk, walker, db)
    assert exc_info.value.status_code == 409


def test_accept_walk_permite_block_de_outro_tenant():
    """Walk de tB + block de tA → bloco não afeta o aceite do tB."""
    db = _db()
    walker = _seed_task5(db)
    walk = _make_walk_t5(db, tenant_id="tB")
    _force_attempt(db, walk, walker)

    # Block é de tA, mas o walk é de tB — não deve bloquear
    _block(db, walker.id, _TASK5_DATE, "09:00", "12:00", tenant_id="tA")

    # Não deve levantar 409
    result = accept_walk(walk, walker, db)
    assert result is not None
