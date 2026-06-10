"""Testes de ROTA (camada HTTP) das 3 rotas novas de Confianca + Gamificacao.

Padrao do projeto (ver tests/test_routes_onda1.py e tests/test_routes_auth.py):
monta um FastAPI MINIMO so com os routers-alvo, SQLite em memoria (StaticPool),
overrides de get_db / get_current_user. NAO importa app.main (que conecta no
banco de PROD).

Cobre:
- GET /walker/me/trust   (walker_trust)        -> compute-on-read; sem perfil = Bronze
- GET /tutors/me/gamification (tutor_gamification) -> XP/nivel a partir dos passeios
- GET /pets/{id}/routine (pet_routine)         -> 404 inexistente, 403 de outro tutor, 200 dono
- 401 sem autenticacao (get_current_user real, sem Authorization header)
"""
from datetime import datetime, timedelta

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
from app.routes import pet_routine, tutor_gamification, walker_trust
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-test"
TUTOR_ID = "tutor-test"
OTHER_TUTOR_ID = "outro-tutor"


def build(*, authenticate=True):
    """Monta app minimo com os 3 routers-alvo e SQLite em memoria isolado.

    Se authenticate=False, NAO sobrescreve get_current_user -> get_current_user
    real, que sem Authorization header (HTTPBearer auto_error=False) responde 401.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="tutor@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(User(id=OTHER_TUTOR_ID, email="outro@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(walker_trust.router)
    test_app.include_router(tutor_gamification.router)
    test_app.include_router(pet_routine.router)
    test_app.dependency_overrides[get_db] = lambda: db
    if authenticate:
        test_app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)
    return TestClient(test_app), db


def add_pet(db, pet_id, tutor_id=TUTOR_ID, **extra):
    pet = Pet(id=pet_id, tutor_id=tutor_id, name=extra.pop("name", pet_id), **extra)
    db.add(pet)
    db.commit()
    return pet


def add_completed_walk(db, pet_id, when: datetime, walk_id, tutor_id=TUTOR_ID):
    db.add(Walk(
        id=walk_id,
        tutor_id=tutor_id,
        pet_id=pet_id,
        scheduled_date=when.strftime("%Y-%m-%dT%H:%M:%S"),
        duration_minutes=45,
        price=30.0,
        status="finalizado",  # esta em COMPLETED_WALK_STATUSES (lowercase) e FINISHED_STATUSES
        operational_status="ride_completed",
    ))
    db.commit()


# ------------------------------------------------------- walker trust --------
def test_walker_trust_requires_auth_401():
    client, _ = build(authenticate=False)
    r = client.get("/walker/me/trust")
    assert r.status_code == 401


def test_walker_trust_no_profile_is_bronze():
    # Passeador sem WalkerProfile / sem reviews: compute-only nao quebra.
    client, _ = build()
    r = client.get("/walker/me/trust")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["walker_user_id"] == TUTOR_ID
    assert body["level"] == "Bronze"
    # Sem perfil/sem documentos -> todos os selos falsos.
    assert body["seals"] == {
        "cadastro_verificado": False,
        "identidade_verificada": False,
        "passeador_verificado": False,
    }
    # 5 certificacoes automaticas, nenhuma concedida sem perfil.
    assert len(body["certifications"]) == 5
    assert all(c["granted"] is False for c in body["certifications"])
    # metrics expostos para o front/admin.
    assert body["metrics"]["total_walks"] == 0
    assert body["metrics"]["is_active"] is False


# --------------------------------------------------- tutor gamification ------
def test_tutor_gamification_requires_auth_401():
    client, _ = build(authenticate=False)
    r = client.get("/tutors/me/gamification")
    assert r.status_code == 401


def test_tutor_gamification_empty_is_level_one():
    client, _ = build()
    r = client.get("/tutors/me/gamification")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tutor_id"] == TUTOR_ID
    assert body["tutor_xp"] == 0
    assert body["tutor_level"] == 1
    assert body["tutor_level_title"] == "Tutor iniciante"
    assert body["total_walks_completed"] == 0
    assert body["total_pets_registered"] == 0
    # 5 badges, todas locked sem atividade.
    assert len(body["badges"]) == 5
    assert all(b["status"] == "locked" for b in body["badges"])
    assert body["recent_events"]  # sempre tem o evento de streak


def test_tutor_gamification_completed_walks_grant_xp_and_badge():
    client, db = build()
    add_pet(db, "rex")
    # 3 passeios concluidos -> XP = 3*45 = 135 (nivel 2) e badge dedicated_tutor.
    base = datetime.utcnow()
    add_completed_walk(db, "rex", base - timedelta(days=2), "w1")
    add_completed_walk(db, "rex", base - timedelta(days=1), "w2")
    add_completed_walk(db, "rex", base, "w3")

    r = client.get("/tutors/me/gamification")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_walks_completed"] == 3
    assert body["tutor_xp"] == 135
    assert body["tutor_level"] == 2
    assert body["total_pets_registered"] == 1
    dedicated = next(b for b in body["badges"] if b["type"] == "dedicated_tutor")
    assert dedicated["status"] == "unlocked"
    first_care_or_family = {b["type"]: b["status"] for b in body["badges"]}
    # familia completa: tem >=1 pet cadastrado.
    assert first_care_or_family["complete_family"] == "unlocked"


# --------------------------------------------------------- pet routine -------
def test_pet_routine_requires_auth_401():
    client, _ = build(authenticate=False)
    r = client.get("/pets/qualquer/routine")
    assert r.status_code == 401


def test_pet_routine_404_when_pet_missing():
    client, _ = build()
    r = client.get("/pets/nao-existe/routine")
    assert r.status_code == 404
    assert "nao encontrado" in r.json()["detail"].lower()


def test_pet_routine_403_for_other_tutors_pet():
    client, db = build()
    add_pet(db, "petalheio", tutor_id=OTHER_TUTOR_ID)
    r = client.get("/pets/petalheio/routine")
    assert r.status_code == 403
    assert "dono" in r.json()["detail"].lower()


def test_pet_routine_200_for_owner_empty():
    client, db = build()
    add_pet(db, "rex", breed="Vira-lata", age=3, size="medium")
    r = client.get("/pets/rex/routine")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pet_id"] == "rex"
    assert body["tutor_id"] == TUTOR_ID
    assert body["name"] == "rex"
    assert body["weekly_walk_count"] == 0
    assert body["xp"] == 0
    assert body["level"] == 1
    # Sem ultimo passeio -> status indefinido.
    assert body["current_status"] == "undefined"
    assert body["last_walk_at"] is None
    assert len(body["badges"]) == 6
    assert all(b["status"] == "locked" for b in body["badges"])


def test_pet_routine_200_with_completed_walks():
    client, db = build()
    add_pet(db, "rex")
    base = datetime.utcnow()
    add_completed_walk(db, "rex", base - timedelta(hours=2), "w1")
    add_completed_walk(db, "rex", base - timedelta(days=1), "w2")

    r = client.get("/pets/rex/routine")
    assert r.status_code == 200, r.text
    body = r.json()
    # 2 passeios concluidos; ambos na semana corrente -> weekly_walk_count contado.
    assert body["last_walk_at"] is not None
    # XP = total*35 + weekly*10. total=2 -> base de 70 + weekly.
    assert body["xp"] >= 2 * 35
    first_walk = next(b for b in body["badges"] if b["type"] == "first_walk")
    assert first_walk["status"] == "unlocked"
    # ultimo passeio ha ~2h -> satisfeito pos-passeio.
    assert body["current_status"] == "post_walk_satisfied"
