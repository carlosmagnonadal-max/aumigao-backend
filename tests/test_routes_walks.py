"""Testes de ROTA (camada HTTP) do modulo app/routes/walks.py.

Padrao do projeto (ver tests/test_routes_onda1.py e tests/test_routes_auth.py):
monta um FastAPI MINIMO com apenas o router de walks, SQLite em memoria
(StaticPool), overrides de get_db / get_current_user. NAO importa app.main
(que conecta no banco de PROD).

Cobre:
- POST /walks (criar passeio: matching adiado -> pending_walker_confirmation)
- GET /walks (listar passeios do tutor) + 401 sem auth
- PUT /walks/{id}/status (transicao livre; bloqueio de finalizacao direta -> 400)
- POST /walks/{id}/review (gating: exige ride_completed + revisao operacional aprovada)
- POST /walks/{id}/tip-checkout (mesmo gating de completion aprovado)
"""
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_completion_review import WalkCompletionReview
from app.routes import walks
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-test"
TUTOR_ID = "tutor-test"
WALKER_ID = "walker-test"
PET_ID = "pet-test"


def build():
    """Monta app minimo com o router de walks e um SQLite em memoria isolado."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # slug = DEFAULT para default_tenant_id() resolver este tenant sem criar outro.
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="tutor@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(User(id=WALKER_ID, email="walker@test.com", password_hash="x", role="walker", tenant_id=TENANT_ID, is_active=True))
    db.add(Pet(id=PET_ID, tutor_id=TUTOR_ID, name="Rex", tenant_id=TENANT_ID))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(walks.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)
    return TestClient(test_app), db


def _walk_create_payload(**extra):
    base = {
        "pet_id": PET_ID,
        "scheduled_date": "2026-07-01T10:00:00",
        "duration_minutes": 45,
        "price": 40.0,
        "pickup_method": "Buscar em casa",
        "address_snapshot": "Rua A, 100 - Centro",
        "notes": "Cuidado com o portao",
    }
    base.update(extra)
    return base


def _seed_completed_walk(db, *, approved_review: bool = True, walker_id: str | None = WALKER_ID,
                         operational_status: str = "ride_completed"):
    """Cria um passeio finalizado operacionalmente, opcionalmente com revisao aprovada."""
    walk = Walk(
        id=str(uuid4()),
        tutor_id=TUTOR_ID,
        tenant_id=TENANT_ID,
        walker_id=walker_id,
        assigned_walker_id=walker_id,
        pet_id=PET_ID,
        scheduled_date="2026-07-01T10:00:00",
        duration_minutes=45,
        price=40.0,
        status="Finalizado",
        operational_status=operational_status,
        walker_selection_mode="auto",
    )
    db.add(walk)
    if approved_review:
        db.add(WalkCompletionReview(
            id=str(uuid4()),
            tenant_id=TENANT_ID,
            walk_id=walk.id,
            walker_user_id=walker_id,
            tutor_user_id=TUTOR_ID,
            status="approved",
        ))
    db.commit()
    return walk


# --------------------------------------------------------------- create -----
def test_create_walk_defers_matching_to_pending_confirmation():
    client, _ = build()
    r = client.post("/walks", json=_walk_create_payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tutor_id"] == TUTOR_ID
    assert body["pet_id"] == PET_ID
    assert body["pet_name"] == "Rex"
    # Matching inicial adiado: passeio nasce aguardando confirmacao, status legado Agendado.
    assert body["operational_status"] == "pending_walker_confirmation"
    assert body["status"] == "Agendado"
    assert body["walker_selection_mode"] == "auto"


def test_create_walk_persists_and_appears_in_list():
    client, db = build()
    created = client.post("/walks", json=_walk_create_payload()).json()
    assert db.get(Walk, created["id"]) is not None
    listed = client.get("/walks")
    assert listed.status_code == 200
    ids = [w["id"] for w in listed.json()]
    assert created["id"] in ids


# ----------------------------------------------------------------- list -----
def test_list_walks_requires_auth_401():
    client, _ = build()
    # remove o override de auth -> HTTPBearer(auto_error=False) -> get_current_user real -> 401
    client.app.dependency_overrides.pop(get_current_user, None)
    r = client.get("/walks")
    assert r.status_code == 401


def test_list_walks_only_returns_own_walks_for_tutor():
    client, db = build()
    # passeio do tutor logado
    client.post("/walks", json=_walk_create_payload())
    # passeio de outro tutor (nao deve aparecer)
    db.add(User(id="other-tutor", email="other@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(Walk(id="other-walk", tutor_id="other-tutor", tenant_id=TENANT_ID, pet_id=PET_ID,
                scheduled_date="2026-07-02T10:00:00", duration_minutes=30, price=20.0,
                status="Agendado", operational_status="ride_scheduled"))
    db.commit()
    listed = client.get("/walks").json()
    tutor_ids = {w["tutor_id"] for w in listed}
    assert tutor_ids == {TUTOR_ID}


# --------------------------------------------------------------- status -----
def test_update_status_blocks_direct_completion():
    client, db = build()
    walk = _seed_completed_walk(db, approved_review=False, operational_status="ride_in_progress")
    r = client.put(f"/walks/{walk.id}/status", json={"status": "Finalizado"})
    assert r.status_code == 400
    assert "revis" in r.json()["detail"].lower()


def test_update_status_blocks_ride_completed_operational_value():
    client, db = build()
    walk = _seed_completed_walk(db, approved_review=False, operational_status="ride_in_progress")
    r = client.put(f"/walks/{walk.id}/status", json={"status": "ride_completed"})
    assert r.status_code == 400


def test_update_status_allows_intermediate_transition():
    client, db = build()
    walk = _seed_completed_walk(db, approved_review=False, operational_status="walker_accepted")
    r = client.put(f"/walks/{walk.id}/status", json={"status": "Indo buscar o pet"})
    assert r.status_code == 200, r.text
    assert r.json()["operational_status"] == "walker_arriving"


# --------------------------------------------------------------- review -----
def test_review_happy_path_after_approved_completion():
    client, db = build()
    walk = _seed_completed_walk(db, approved_review=True)
    r = client.post(f"/walks/{walk.id}/review", json={
        "rating": 5, "comment": "Otimo passeio", "tags": ["punctual", "caring"],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["review"]["rating"] == 5
    assert sorted(body["review"]["tags"]) == ["caring", "punctual"]


def test_review_blocked_without_completed_status():
    client, db = build()
    # revisao aprovada existe, mas o passeio ainda nao esta ride_completed
    walk = _seed_completed_walk(db, approved_review=True, operational_status="ride_in_progress")
    r = client.post(f"/walks/{walk.id}/review", json={"rating": 4})
    assert r.status_code == 409


def test_review_blocked_without_approved_completion_review():
    client, db = build()
    # ride_completed mas SEM WalkCompletionReview aprovada
    walk = _seed_completed_walk(db, approved_review=False)
    r = client.post(f"/walks/{walk.id}/review", json={"rating": 4})
    assert r.status_code == 409


def test_review_rejects_non_owner_tutor_403():
    client, db = build()
    walk = _seed_completed_walk(db, approved_review=True)
    # outro tutor tenta avaliar passeio que nao e dele -> _get_walk_for_user nega com 403
    db.add(User(id="intruder", email="intruder@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.commit()
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, "intruder")
    r = client.post(f"/walks/{walk.id}/review", json={"rating": 5})
    assert r.status_code == 403


# ----------------------------------------------------------- tip-checkout ----
def test_tip_checkout_happy_path_after_approved_completion():
    client, db = build()
    walk = _seed_completed_walk(db, approved_review=True)
    r = client.post(f"/walks/{walk.id}/tip-checkout", json={"amount": 10.0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["tip_id"]
    assert body["checkout_url"].startswith("aumigao://tip-checkout/")


def test_tip_checkout_blocked_without_completed_status():
    client, db = build()
    walk = _seed_completed_walk(db, approved_review=True, operational_status="ride_in_progress")
    r = client.post(f"/walks/{walk.id}/tip-checkout", json={"amount": 10.0})
    assert r.status_code == 409


def test_tip_checkout_blocked_without_approved_completion_review():
    client, db = build()
    walk = _seed_completed_walk(db, approved_review=False)
    r = client.post(f"/walks/{walk.id}/tip-checkout", json={"amount": 10.0})
    assert r.status_code == 409


def test_tip_checkout_requires_auth_401():
    client, db = build()
    walk = _seed_completed_walk(db, approved_review=True)
    client.app.dependency_overrides.pop(get_current_user, None)
    r = client.post(f"/walks/{walk.id}/tip-checkout", json={"amount": 10.0})
    assert r.status_code == 401
