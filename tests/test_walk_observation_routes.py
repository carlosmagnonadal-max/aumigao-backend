"""T10 — Testes da rota POST /api/walks/{walk_id}/observation."""
from __future__ import annotations

import app.models  # noqa: F401

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.pet_profile_config import PetProfileConfig
from app.models.pet_timeline_event import PetTimelineEvent
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_observation import WalkObservation
from app.routes import pet_profile as routes


def _ctx(obs_active=True):
    """Cria DB em memória com tenant, user tutor, walker, pet, walk e feature toggles."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()

    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan="business"))
    db.add(User(id="tutor1", email="tutor@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.add(User(id="walker1", email="walker@x.com", password_hash="x", role="walker", tenant_id="t1"))
    db.add(User(id="other1", email="other@x.com", password_hash="x", role="walker", tenant_id="t1"))
    db.add(Pet(id="p1", tutor_id="tutor1", tenant_id="t1", name="Rex"))
    db.add(Walk(
        id="w1", tutor_id="tutor1", pet_id="p1", tenant_id="t1",
        walker_id="walker1",
        scheduled_date="2026-07-01", duration_minutes=30, price=50.0,
    ))

    if obs_active:
        db.add(TenantFeature(tenant_id="t1", feature_key="walk_observations_form", enabled=True))
        db.add(PetProfileConfig(tenant_id="t1", observations_enabled=True))

    db.commit()
    return db


def _client(db, user, env_on: bool, monkeypatch):
    if env_on:
        monkeypatch.setenv("WALK_OBSERVATIONS_ENABLED", "true")
    else:
        monkeypatch.delenv("WALK_OBSERVATIONS_ENABLED", raising=False)

    app = FastAPI()
    app.include_router(routes.api_walk_obs_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


_PAYLOAD = {
    "mood": "calm",
    "energy": "normal",
    "peed": True,
    "pooped": False,
    "incident": False,
    "incident_notes": "",
}


def test_walker_creates_observation_201(monkeypatch):
    """(a) Walker do passeio com feature ON → 201 cria observação + timeline."""
    db = _ctx(obs_active=True)
    walker = db.get(User, "walker1")
    c = _client(db, walker, env_on=True, monkeypatch=monkeypatch)

    r = c.post("/api/walks/w1/observation", json=_PAYLOAD)
    assert r.status_code == 201, r.text
    body = r.json()
    assert "observation" in body
    assert body["observation"]["walk_id"] == "w1"
    assert body["observation"]["mood"] == "calm"

    assert db.query(WalkObservation).count() == 1
    assert db.query(PetTimelineEvent).filter(
        PetTimelineEvent.event_type == "walk_observation"
    ).count() == 1


def test_feature_off_returns_404(monkeypatch):
    """(b) Feature OFF → 404."""
    db = _ctx(obs_active=False)
    walker = db.get(User, "walker1")
    c = _client(db, walker, env_on=False, monkeypatch=monkeypatch)

    r = c.post("/api/walks/w1/observation", json=_PAYLOAD)
    assert r.status_code == 404


def test_non_walker_returns_403(monkeypatch):
    """(c) User que NÃO é o passeador → 403."""
    db = _ctx(obs_active=True)
    other = db.get(User, "other1")
    c = _client(db, other, env_on=True, monkeypatch=monkeypatch)

    r = c.post("/api/walks/w1/observation", json=_PAYLOAD)
    assert r.status_code == 403


def test_idempotency_no_duplicate(monkeypatch):
    """(d) POST 2x → sem duplicar WalkObservation nem PetTimelineEvent."""
    db = _ctx(obs_active=True)
    walker = db.get(User, "walker1")
    c = _client(db, walker, env_on=True, monkeypatch=monkeypatch)

    r1 = c.post("/api/walks/w1/observation", json=_PAYLOAD)
    assert r1.status_code == 201
    r2 = c.post("/api/walks/w1/observation", json={**_PAYLOAD, "mood": "happy"})
    assert r2.status_code == 201

    assert db.query(WalkObservation).count() == 1
    assert db.query(PetTimelineEvent).count() == 1
    # Deve ter atualizado o mood
    obs = db.query(WalkObservation).first()
    assert obs.mood == "happy"


def test_walk_not_found_returns_404(monkeypatch):
    """(e) Walk inexistente → 404."""
    db = _ctx(obs_active=True)
    walker = db.get(User, "walker1")
    c = _client(db, walker, env_on=True, monkeypatch=monkeypatch)

    r = c.post("/api/walks/nao-existe/observation", json=_PAYLOAD)
    assert r.status_code == 404


def test_assigned_walker_can_submit(monkeypatch):
    """assigned_walker_id também tem permissão de enviar observação."""
    db = _ctx(obs_active=True)
    # Troca: assigned_walker_id = other1 (não é walker_id)
    walk = db.get(Walk, "w1")
    walk.walker_id = None
    walk.assigned_walker_id = "other1"
    db.commit()

    other = db.get(User, "other1")
    c = _client(db, other, env_on=True, monkeypatch=monkeypatch)

    r = c.post("/api/walks/w1/observation", json=_PAYLOAD)
    assert r.status_code == 201


def test_invalid_mood_returns_422(monkeypatch):
    """mood com valor fora do enum → 422."""
    db = _ctx(obs_active=True)
    walker = db.get(User, "walker1")
    c = _client(db, walker, env_on=True, monkeypatch=monkeypatch)

    r = c.post("/api/walks/w1/observation", json={**_PAYLOAD, "mood": "INVALIDO"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Review P2 #1 — 404 sem vazamento de existência
# ---------------------------------------------------------------------------

def test_feature_off_404_body_identical_for_existing_and_missing_walk(monkeypatch):
    """Com feature OFF, walk existente e inexistente retornam 404 com corpo IDÊNTICO."""
    db = _ctx(obs_active=False)
    walker = db.get(User, "walker1")
    c = _client(db, walker, env_on=False, monkeypatch=monkeypatch)

    r_existing = c.post("/api/walks/w1/observation", json=_PAYLOAD)
    r_missing = c.post("/api/walks/nao-existe/observation", json=_PAYLOAD)

    assert r_existing.status_code == 404
    assert r_missing.status_code == 404
    assert r_existing.json() == r_missing.json()


# ---------------------------------------------------------------------------
# Review P2 #2 — race do double-POST: IntegrityError → retorna a existente
# ---------------------------------------------------------------------------

def test_integrity_error_race_returns_existing_observation(monkeypatch):
    """Se a persistência estourar IntegrityError (double-POST concorrente),
    o handler faz rollback, re-busca a observação existente e retorna sem 500."""
    from sqlalchemy.exc import IntegrityError
    from app.services import pet_profile_service as svc_module

    db = _ctx(obs_active=True)
    walker = db.get(User, "walker1")

    # Pré-insere a observação "do outro request" que venceu a corrida.
    pre = WalkObservation(walk_id="w1", pet_id="p1", tenant_id="t1",
                          walker_user_id="walker1", mood="happy")
    db.add(pre)
    db.commit()
    pre_id = pre.id

    def _raise_integrity(*args, **kwargs):
        raise IntegrityError("INSERT INTO walk_observations", {}, Exception("UNIQUE constraint failed"))

    monkeypatch.setattr(svc_module, "record_walk_observation", _raise_integrity)

    c = _client(db, walker, env_on=True, monkeypatch=monkeypatch)
    r = c.post("/api/walks/w1/observation", json=_PAYLOAD)

    assert r.status_code == 201, r.text
    assert r.json()["observation"]["id"] == pre_id
    assert db.query(WalkObservation).count() == 1


# ---------------------------------------------------------------------------
# Review P2 #3 — campo aditivo walk_observations_enabled no GET /walks/{id}
# ---------------------------------------------------------------------------

def _walks_client(db, user):
    from app.routes import walks as walks_routes
    app = FastAPI()
    app.include_router(walks_routes.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def test_get_walk_observations_enabled_false_when_feature_off(monkeypatch):
    db = _ctx(obs_active=False)
    monkeypatch.delenv("WALK_OBSERVATIONS_ENABLED", raising=False)
    tutor = db.get(User, "tutor1")
    c = _walks_client(db, tutor)

    r = c.get("/walks/w1")
    assert r.status_code == 200, r.text
    assert r.json()["walk_observations_enabled"] is False


def test_get_walk_observations_enabled_true_when_feature_on(monkeypatch):
    db = _ctx(obs_active=True)
    monkeypatch.setenv("WALK_OBSERVATIONS_ENABLED", "true")
    tutor = db.get(User, "tutor1")
    c = _walks_client(db, tutor)

    r = c.get("/walks/w1")
    assert r.status_code == 200, r.text
    assert r.json()["walk_observations_enabled"] is True
