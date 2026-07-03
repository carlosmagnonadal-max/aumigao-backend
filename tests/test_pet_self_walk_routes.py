"""Testes de rota da Fase D — self-walks: gating, ownership, validações, rate-limit."""
from __future__ import annotations

import app.models  # noqa: F401

from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.pet_profile_config import PetProfileConfig
from app.models.pet_self_walk import MAX_SELF_WALKS_PER_DAY
from app.models.pet_timeline_event import PetTimelineEvent
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.routes import pet_self_walk as routes


def _ctx(active=True, plan="pro", with_trial=False):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    trial_ends = datetime.utcnow() + timedelta(days=10) if with_trial else None
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan=plan, trial_ends_at=trial_ends))
    db.add(User(id="u1", email="u1@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.add(User(id="u2", email="u2@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.add(User(id="adm", email="adm@x.com", password_hash="x", role="admin", tenant_id="t1"))
    db.add(Pet(id="p1", tutor_id="u1", tenant_id="t1", name="Rex"))
    if active:
        db.add(TenantFeature(tenant_id="t1", feature_key="pet_live_profile", enabled=True))
        db.add(PetProfileConfig(tenant_id="t1", profile_enabled=True))
    db.commit()
    return db


def _client(db, user, env, monkeypatch):
    if env:
        monkeypatch.setenv("PET_LIVE_PROFILE_ENABLED", "true")
    else:
        monkeypatch.delenv("PET_LIVE_PROFILE_ENABLED", raising=False)
    app = FastAPI()
    app.include_router(routes.api_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _payload(**over):
    body = {
        "started_at": (datetime.utcnow() - timedelta(hours=1)).replace(microsecond=0).isoformat(),
        "duration_seconds": 1800,
        "distance_km": 1.4,
        "walk_type": "rua",
        "intensity": "moderado",
        "had_gps": True,
        "needs": {"pee": True, "poop": False, "water": True},
        "behavior": {"interacted_dogs": False, "interacted_people": True, "pulled_leash": False,
                     "showed_fear": False, "showed_reactivity": False},
        "notes": "tranquilo",
    }
    body.update(over)
    return body


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

def test_dormant_returns_404(monkeypatch):
    db = _ctx(active=False)
    c = _client(db, db.get(User, "u1"), env=False, monkeypatch=monkeypatch)
    assert c.get("/api/pets/p1/self-walks").status_code == 404


def test_free_plan_returns_403_teaser(monkeypatch):
    db = _ctx(active=True, plan="free", with_trial=False)
    c = _client(db, db.get(User, "u1"), env=True, monkeypatch=monkeypatch)
    r = c.post("/api/pets/p1/self-walks", json=_payload())
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "plan_upgrade_required"


def test_free_plan_trial_active_allows(monkeypatch):
    db = _ctx(active=True, plan="free", with_trial=True)
    c = _client(db, db.get(User, "u1"), env=True, monkeypatch=monkeypatch)
    assert c.post("/api/pets/p1/self-walks", json=_payload()).status_code == 201


# ---------------------------------------------------------------------------
# Ownership (self-serve: só o tutor dono)
# ---------------------------------------------------------------------------

def test_other_tutor_404(monkeypatch):
    db = _ctx(active=True)
    c = _client(db, db.get(User, "u2"), env=True, monkeypatch=monkeypatch)
    assert c.post("/api/pets/p1/self-walks", json=_payload()).status_code == 404


def test_admin_not_owner_404(monkeypatch):
    """Admin do tenant NÃO registra self-walk (é exclusivo do tutor dono)."""
    db = _ctx(active=True)
    c = _client(db, db.get(User, "adm"), env=True, monkeypatch=monkeypatch)
    assert c.post("/api/pets/p1/self-walks", json=_payload()).status_code == 404


# ---------------------------------------------------------------------------
# CRUD + contrato + timeline
# ---------------------------------------------------------------------------

def test_create_returns_contract_and_emits_timeline(monkeypatch):
    db = _ctx(active=True)
    c = _client(db, db.get(User, "u1"), env=True, monkeypatch=monkeypatch)
    r = c.post("/api/pets/p1/self-walks", json=_payload())
    assert r.status_code == 201
    sw = r.json()["self_walk"]
    assert set(sw.keys()) == {
        "id", "pet_id", "started_at", "duration_seconds", "distance_km",
        "walk_type", "intensity", "had_gps", "needs", "behavior", "notes", "created_at",
    }
    assert sw["needs"] == {"pee": True, "poop": False, "water": True}
    assert sw["behavior"]["interacted_people"] is True
    # Evento na timeline.
    ev = db.query(PetTimelineEvent).filter(PetTimelineEvent.pet_id == "p1").all()
    assert len(ev) == 1 and ev[0].event_type == "self_walk"


def test_list_desc(monkeypatch):
    db = _ctx(active=True)
    c = _client(db, db.get(User, "u1"), env=True, monkeypatch=monkeypatch)
    older = (datetime.utcnow() - timedelta(hours=10)).replace(microsecond=0).isoformat()
    c.post("/api/pets/p1/self-walks", json=_payload(started_at=older))
    c.post("/api/pets/p1/self-walks", json=_payload())
    rows = c.get("/api/pets/p1/self-walks").json()["self_walks"]
    assert len(rows) == 2
    assert rows[0]["started_at"] > rows[1]["started_at"]


def test_delete_204(monkeypatch):
    db = _ctx(active=True)
    c = _client(db, db.get(User, "u1"), env=True, monkeypatch=monkeypatch)
    sid = c.post("/api/pets/p1/self-walks", json=_payload()).json()["self_walk"]["id"]
    assert c.delete(f"/api/pets/p1/self-walks/{sid}").status_code == 204
    assert len(c.get("/api/pets/p1/self-walks").json()["self_walks"]) == 0


def test_delete_unknown_404(monkeypatch):
    db = _ctx(active=True)
    c = _client(db, db.get(User, "u1"), env=True, monkeypatch=monkeypatch)
    assert c.delete("/api/pets/p1/self-walks/nope").status_code == 404


# ---------------------------------------------------------------------------
# Validações (422)
# ---------------------------------------------------------------------------

def test_duration_too_short_422(monkeypatch):
    db = _ctx(active=True)
    c = _client(db, db.get(User, "u1"), env=True, monkeypatch=monkeypatch)
    assert c.post("/api/pets/p1/self-walks", json=_payload(duration_seconds=30)).status_code == 422


def test_duration_too_long_422(monkeypatch):
    db = _ctx(active=True)
    c = _client(db, db.get(User, "u1"), env=True, monkeypatch=monkeypatch)
    assert c.post("/api/pets/p1/self-walks", json=_payload(duration_seconds=99999)).status_code == 422


def test_distance_too_far_422(monkeypatch):
    db = _ctx(active=True)
    c = _client(db, db.get(User, "u1"), env=True, monkeypatch=monkeypatch)
    assert c.post("/api/pets/p1/self-walks", json=_payload(distance_km=50)).status_code == 422


def test_distance_null_ok(monkeypatch):
    db = _ctx(active=True)
    c = _client(db, db.get(User, "u1"), env=True, monkeypatch=monkeypatch)
    r = c.post("/api/pets/p1/self-walks", json=_payload(distance_km=None))
    assert r.status_code == 201 and r.json()["self_walk"]["distance_km"] is None


def test_bad_walk_type_422(monkeypatch):
    db = _ctx(active=True)
    c = _client(db, db.get(User, "u1"), env=True, monkeypatch=monkeypatch)
    assert c.post("/api/pets/p1/self-walks", json=_payload(walk_type="lua")).status_code == 422


def test_bad_intensity_422(monkeypatch):
    db = _ctx(active=True)
    c = _client(db, db.get(User, "u1"), env=True, monkeypatch=monkeypatch)
    assert c.post("/api/pets/p1/self-walks", json=_payload(intensity="turbo")).status_code == 422


def test_started_at_future_422(monkeypatch):
    db = _ctx(active=True)
    c = _client(db, db.get(User, "u1"), env=True, monkeypatch=monkeypatch)
    future = (datetime.utcnow() + timedelta(hours=2)).replace(microsecond=0).isoformat()
    assert c.post("/api/pets/p1/self-walks", json=_payload(started_at=future)).status_code == 422


def test_started_at_too_old_422(monkeypatch):
    db = _ctx(active=True)
    c = _client(db, db.get(User, "u1"), env=True, monkeypatch=monkeypatch)
    old = (datetime.utcnow() - timedelta(hours=60)).replace(microsecond=0).isoformat()
    assert c.post("/api/pets/p1/self-walks", json=_payload(started_at=old)).status_code == 422


def test_notes_too_long_422(monkeypatch):
    db = _ctx(active=True)
    c = _client(db, db.get(User, "u1"), env=True, monkeypatch=monkeypatch)
    assert c.post("/api/pets/p1/self-walks", json=_payload(notes="x" * 1001)).status_code == 422


# ---------------------------------------------------------------------------
# Rate-limit (429)
# ---------------------------------------------------------------------------

def test_rate_limit_429(monkeypatch):
    db = _ctx(active=True)
    c = _client(db, db.get(User, "u1"), env=True, monkeypatch=monkeypatch)
    # Âncora no início do dia UTC de hoje + minutos (garante MESMO dia e passado —
    # o dia atual sempre começou <=24h atrás, dentro da janela de 48h).
    now = datetime.utcnow().replace(microsecond=0)
    day_start = now.replace(hour=0, minute=0, second=0)
    for i in range(MAX_SELF_WALKS_PER_DAY):
        started = (day_start + timedelta(minutes=i)).isoformat()
        assert c.post("/api/pets/p1/self-walks", json=_payload(started_at=started)).status_code == 201
    extra = (day_start + timedelta(minutes=MAX_SELF_WALKS_PER_DAY)).isoformat()
    assert c.post("/api/pets/p1/self-walks", json=_payload(started_at=extra)).status_code == 429
