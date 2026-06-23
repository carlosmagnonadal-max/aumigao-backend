"""TDD — disponibilidade por tenant (F3.1).

Task 2: resolver is_walker_available_at ciente de tenant.
Task 3: endpoints POST /walker/availability/exceptions aceitam/retornam tenant_id.
"""
import json
from datetime import datetime, date
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import Base, get_db, get_walker_self_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.models.tenant import Tenant
from app.models.tenant_walker_access import TenantWalkerAccess
from app.models.walker_availability import WalkerAvailability
from app.models.walker_availability_exception import WalkerAvailabilityException
from app.services.walker_availability_service import is_walker_available_at
from app.routes import walker as walker_routes


# ---------------------------------------------------------------------------
# Helpers de banco
# ---------------------------------------------------------------------------

def _db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed_recurring(db, walker_id, day_key="Seg", slots=("09:00",)):
    db.add(WalkerAvailability(
        walker_user_id=walker_id,
        schedule_json=json.dumps({day_key: {"enabled": True, "slots": list(slots)}}),
    ))
    db.commit()


def _exc(db, walker_id, d, kind, start=None, end=None, tenant_id=None):
    db.add(WalkerAvailabilityException(
        id=str(uuid4()), walker_user_id=walker_id, exception_date=d,
        kind=kind, start_time=start, end_time=end, tenant_id=tenant_id,
    ))
    db.commit()


# 2026-06-22 é uma SEGUNDA-feira (weekday()==0 → "Seg").
MONDAY = date(2026, 6, 22)
AT_9 = datetime(2026, 6, 22, 9, 0)


# ---------------------------------------------------------------------------
# Task 2 — Resolver ciente de tenant
# ---------------------------------------------------------------------------

def test_global_block_aplica_a_qualquer_tenant():
    db = _db()
    _seed_recurring(db, "w1")
    _exc(db, "w1", MONDAY, "block", "09:00", "10:00", tenant_id=None)
    assert is_walker_available_at(db, "w1", AT_9) is False
    assert is_walker_available_at(db, "w1", AT_9, tenant_id="tA") is False


def test_block_de_tenant_so_afeta_aquele_tenant():
    db = _db()
    _seed_recurring(db, "w1")
    _exc(db, "w1", MONDAY, "block", "09:00", "10:00", tenant_id="tA")
    # sob tenant A: bloqueado; sob tenant B: disponível (recorrente); sem tenant: disponível (só globais)
    assert is_walker_available_at(db, "w1", AT_9, tenant_id="tA") is False
    assert is_walker_available_at(db, "w1", AT_9, tenant_id="tB") is True
    assert is_walker_available_at(db, "w1", AT_9) is True


def test_open_de_tenant_so_inclui_aquele_tenant():
    db = _db()
    # sem recorrente nesse horário; open extra só pro tenant A às 14h
    AT_14 = datetime(2026, 6, 22, 14, 0)
    _exc(db, "w1", MONDAY, "open", "14:00", "15:00", tenant_id="tA")
    assert is_walker_available_at(db, "w1", AT_14, tenant_id="tA") is True
    assert is_walker_available_at(db, "w1", AT_14, tenant_id="tB") is False
    assert is_walker_available_at(db, "w1", AT_14) is False


def test_sem_tenant_e_comportamento_legado():
    db = _db()
    # exceção de tenant NÃO afeta a chamada legada (sem tenant)
    _seed_recurring(db, "w1")
    _exc(db, "w1", MONDAY, "block", "09:00", "10:00", tenant_id="tA")
    assert is_walker_available_at(db, "w1", AT_9) is True  # só globais → recorrente vale


# ---------------------------------------------------------------------------
# Task 3 — Endpoints aceitam/retornam tenant_id
# ---------------------------------------------------------------------------

def _build_client():
    """Monta cliente HTTP com walker w1 autenticado e um tenant 'tA' vinculado."""
    db = _db()

    # Tenant real (necessário para FK do tenant_id e para TenantWalkerAccess)
    db.add(Tenant(id="tA", name="Tenant A", slug="tenant-a"))
    db.commit()

    # Walker user
    user = User(
        id="w1",
        email="w1@t.invalid",
        password_hash="x",
        role="walker",
        is_active=True,
        token_version=0,
        must_change_password=False,
    )
    db.add(user)
    # Profile ativo
    profile = WalkerProfile(id="p1", user_id="w1", status="active", active_as_walker=True)
    db.add(profile)
    # Vínculo ativo com tA (necessário para is_walker_eligible_for_tenant retornar True)
    db.add(TenantWalkerAccess(
        id=str(uuid4()),
        tenant_id="tA",
        walker_user_id="w1",
        status="active",
        access_type="shared_network",
    ))
    db.commit()

    app = FastAPI()
    app.include_router(walker_routes.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_walker_self_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, "w1")
    return TestClient(app), db


def test_post_exception_aceita_tenant_id():
    client, _ = _build_client()
    resp = client.post(
        "/walker/availability/exceptions",
        json={"exception_date": "2026-06-22", "kind": "block",
              "start_time": "09:00", "end_time": "10:00", "tenant_id": "tA"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] == "tA"


def test_post_exception_sem_tenant_e_global():
    client, _ = _build_client()
    resp = client.post(
        "/walker/availability/exceptions",
        json={"exception_date": "2026-06-22", "kind": "block"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] is None


def test_post_exception_rejeita_tenant_sem_vinculo():
    client, _ = _build_client()
    resp = client.post(
        "/walker/availability/exceptions",
        json={"exception_date": "2026-06-22", "kind": "block", "tenant_id": "tenant-sem-vinculo"},
    )
    assert resp.status_code == 403, resp.text
