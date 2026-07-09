"""Testes para os endpoints de ações do meio do passeio (check-in, pet-handover,
start-checklist, checkin-checklist, experience, active).

Padrão: FastAPI mínimo + SQLite em memória (StaticPool), sem importar app.main.

Cobertura:
- POST /walker/walks/{id}/check-in          → walker_arriving
- POST /walker/walks/{id}/pet-handover      → pet_handover_confirmed
- POST /walker/walks/{id}/start-checklist   → ride_in_progress
- POST /walker/walks/{id}/checkin-checklist → evento registrado, sem transição
- POST /walker/walks/{id}/experience        → retorna did_pee/did_poop
- GET  /walker/walks/active                 → walk ativo atual
- Transição inválida → 409
- Walk de outro walker → 403
- Walk inexistente → 404
"""

import uuid
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.user import User
from app.models.walk import Walk
from app.models.walker_profile import WalkerProfile
from app.routes import walker as walker_routes

WALKER_ID = "walker-test-1"
WALKER2_ID = "walker-test-2"
TUTOR_ID = "tutor-test-1"
PET_ID = "pet-test-1"
WALK_ID = "walk-mid-1"


# ---------------------------------------------------------------------------
# Infra: engine SQLite em memória + app mínimo
# ---------------------------------------------------------------------------

def _make_engine():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _seed(db):
    db.add(User(
        id=WALKER_ID, email="walker@test.com", password_hash="x",
        role="walker", full_name="Walker Teste",
    ))
    db.add(User(
        id=WALKER2_ID, email="walker2@test.com", password_hash="x",
        role="walker", full_name="Walker Dois",
    ))
    db.add(User(
        id=TUTOR_ID, email="tutor@test.com", password_hash="x",
        role="tutor", full_name="Tutor Teste",
    ))
    db.add(WalkerProfile(
        id="wp-1", user_id=WALKER_ID, status="active", active_as_walker=True,
        full_name="Walker Teste",
    ))
    db.add(WalkerProfile(
        id="wp-2", user_id=WALKER2_ID, status="active", active_as_walker=True,
        full_name="Walker Dois",
    ))
    db.add(Pet(id=PET_ID, name="Rex", species="Cachorro", tutor_id=TUTOR_ID))
    db.commit()


def _add_walk(db, walk_id=WALK_ID, walker_id=WALKER_ID, op_status="walker_accepted", status="Agendado"):
    walk = Walk(
        id=walk_id,
        tutor_id=TUTOR_ID,
        walker_id=walker_id,
        pet_id=PET_ID,
        scheduled_date="2026-06-20T10:00:00",
        duration_minutes=30,
        price=50.0,
        status=status,
        operational_status=op_status,
    )
    db.add(walk)
    db.commit()
    return walk


def _build_app(db, current_user_id=WALKER_ID):
    test_app = FastAPI()
    test_app.include_router(walker_routes.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, current_user_id)
    return TestClient(test_app)


@pytest.fixture()
def setup():
    engine = _make_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    _seed(db)
    yield db
    db.close()


# ---------------------------------------------------------------------------
# CHECK-IN
# ---------------------------------------------------------------------------

def test_check_in_happy_path(setup):
    db = setup
    _add_walk(db, op_status="walker_accepted")
    client = _build_app(db)
    resp = client.post(f"/walker/walks/{WALK_ID}/check-in", json={
        "checklist_confirm_water": True,
        "checklist_confirm_bowl": True,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["checked_in"] is True
    assert data["operational_status"] == "walker_arriving"
    assert data["status"] == "Indo buscar o pet"
    assert data["id"] == WALK_ID


def test_check_in_from_ride_scheduled(setup):
    db = setup
    _add_walk(db, op_status="ride_scheduled")
    client = _build_app(db)
    resp = client.post(f"/walker/walks/{WALK_ID}/check-in", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["operational_status"] == "walker_arriving"


def test_check_in_idempotent_from_walker_arriving(setup):
    db = setup
    _add_walk(db, op_status="walker_arriving", status="Indo buscar o pet")
    client = _build_app(db)
    resp = client.post(f"/walker/walks/{WALK_ID}/check-in", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["operational_status"] == "walker_arriving"


def test_check_in_invalid_transition(setup):
    """Não deve fazer check-in se já está em ride_in_progress."""
    db = setup
    _add_walk(db, op_status="ride_in_progress", status="Passeando agora")
    client = _build_app(db)
    resp = client.post(f"/walker/walks/{WALK_ID}/check-in", json={})
    assert resp.status_code == 409, resp.text


def test_check_in_walk_not_found(setup):
    db = setup
    client = _build_app(db)
    resp = client.post("/walker/walks/nonexistent-id/check-in", json={})
    assert resp.status_code == 404, resp.text


def test_check_in_wrong_walker(setup):
    db = setup
    _add_walk(db, walker_id=WALKER2_ID, op_status="walker_accepted")
    # Walker 1 tenta fazer check-in no walk do Walker 2
    client = _build_app(db, current_user_id=WALKER_ID)
    resp = client.post(f"/walker/walks/{WALK_ID}/check-in", json={})
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# PET HANDOVER
# ---------------------------------------------------------------------------

def test_pet_handover_happy_path(setup):
    db = setup
    walk = _add_walk(db, op_status="walker_arriving", status="Indo buscar o pet")
    client = _build_app(db)
    # Código de Coleta (mig 0105): handover exige o código que o tutor informa.
    resp = client.post(f"/walker/walks/{WALK_ID}/pet-handover", json={"security_code": walk.security_code})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["confirmed"] is True
    assert data["operational_status"] == "pet_handover_confirmed"


def test_pet_handover_invalid_transition(setup):
    """Não deve confirmar handover sem ter feito check-in antes."""
    db = setup
    _add_walk(db, op_status="walker_accepted")
    client = _build_app(db)
    resp = client.post(f"/walker/walks/{WALK_ID}/pet-handover")
    assert resp.status_code == 409, resp.text


# ---------------------------------------------------------------------------
# START CHECKLIST
# ---------------------------------------------------------------------------

def test_start_checklist_happy_path(setup):
    db = setup
    _add_walk(db, op_status="pet_handover_confirmed", status="Indo buscar o pet")
    client = _build_app(db)
    resp = client.post(f"/walker/walks/{WALK_ID}/start-checklist", json={
        "checklist_confirm_water": True,
        "checklist_confirm_bowl": True,
        "checklist_confirm_bags": True,
        "checklist_confirm_first_aid": True,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["operational_status"] == "ride_in_progress"
    assert data["status"] == "Passeando agora"
    assert data["kit_checklist_start_confirmed"] is True


def test_start_checklist_invalid_transition(setup):
    """Não pode fazer start-checklist sem handover do pet."""
    db = setup
    _add_walk(db, op_status="walker_arriving")
    client = _build_app(db)
    resp = client.post(f"/walker/walks/{WALK_ID}/start-checklist", json={})
    assert resp.status_code == 409, resp.text


# ---------------------------------------------------------------------------
# CHECKIN CHECKLIST
# ---------------------------------------------------------------------------

def test_checkin_checklist_happy_path(setup):
    db = setup
    _add_walk(db, op_status="walker_arriving", status="Indo buscar o pet")
    client = _build_app(db)
    resp = client.post(f"/walker/walks/{WALK_ID}/checkin-checklist", json={
        "checklist_confirm_water": True,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["kit_checklist_check_in_confirmed"] is True
    # Status não muda
    assert data["operational_status"] == "walker_arriving"


def test_checkin_checklist_invalid_status(setup):
    """Não pode validar checklist de chegada em ride_completed."""
    db = setup
    _add_walk(db, op_status="ride_completed", status="Finalizado")
    client = _build_app(db)
    resp = client.post(f"/walker/walks/{WALK_ID}/checkin-checklist", json={})
    assert resp.status_code == 409, resp.text


# ---------------------------------------------------------------------------
# EXPERIENCE
# ---------------------------------------------------------------------------

def test_experience_happy_path(setup):
    db = setup
    _add_walk(db, op_status="ride_in_progress", status="Passeando agora")
    client = _build_app(db)
    resp = client.post(f"/walker/walks/{WALK_ID}/experience", json={
        "did_pee": True, "did_poop": False,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["did_pee"] is True
    assert data["did_poop"] is False
    assert data["id"] == WALK_ID


def test_experience_invalid_status(setup):
    """Não pode registrar experiência em walker_accepted (antes do início)."""
    db = setup
    _add_walk(db, op_status="walker_accepted")
    client = _build_app(db)
    resp = client.post(f"/walker/walks/{WALK_ID}/experience", json={
        "did_pee": True,
    })
    assert resp.status_code == 409, resp.text


# ---------------------------------------------------------------------------
# ACTIVE WALK
# ---------------------------------------------------------------------------

def test_active_walk_found(setup):
    db = setup
    _add_walk(db, op_status="ride_in_progress", status="Passeando agora")
    client = _build_app(db)
    resp = client.get("/walker/walks/active")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["id"] == WALK_ID
    assert data["operational_status"] == "ride_in_progress"


def test_active_walk_not_found(setup):
    db = setup
    # Nenhum walk ativo criado
    client = _build_app(db)
    resp = client.get("/walker/walks/active")
    assert resp.status_code == 404, resp.text


def test_active_walk_walker_arriving_counts(setup):
    db = setup
    _add_walk(db, op_status="walker_arriving", status="Indo buscar o pet")
    client = _build_app(db)
    resp = client.get("/walker/walks/active")
    assert resp.status_code == 200, resp.text
    assert resp.json()["operational_status"] == "walker_arriving"


# ---------------------------------------------------------------------------
# FULL FLOW: walker_accepted → walker_arriving → pet_handover_confirmed
#            → ride_in_progress
# ---------------------------------------------------------------------------

def test_full_mid_walk_flow(setup):
    db = setup
    walk = _add_walk(db, op_status="walker_accepted")
    client = _build_app(db)

    # 1. Check-in
    r1 = client.post(f"/walker/walks/{WALK_ID}/check-in", json={})
    assert r1.status_code == 200
    assert r1.json()["operational_status"] == "walker_arriving"

    # 2. Pet handover — com o Código de Coleta (mig 0105) informado pelo tutor
    r2 = client.post(f"/walker/walks/{WALK_ID}/pet-handover", json={"security_code": walk.security_code})
    assert r2.status_code == 200
    assert r2.json()["operational_status"] == "pet_handover_confirmed"

    # 3. Start checklist
    r3 = client.post(f"/walker/walks/{WALK_ID}/start-checklist", json={})
    assert r3.status_code == 200
    assert r3.json()["operational_status"] == "ride_in_progress"

    # 4. Experiência
    r4 = client.post(f"/walker/walks/{WALK_ID}/experience", json={"did_pee": True, "did_poop": True})
    assert r4.status_code == 200
    assert r4.json()["did_pee"] is True

    # 5. Active walk
    r5 = client.get("/walker/walks/active")
    assert r5.status_code == 200
    assert r5.json()["operational_status"] == "ride_in_progress"
