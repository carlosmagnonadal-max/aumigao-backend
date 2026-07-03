"""Testes de rota das Conquistas — GET /api/pets/{pet_id}/achievements (Fase C).

Mesmo gate/ownership de A/B: tutor dono / admin do tenant; feature ativa + plano
Pro+ (free → 403 teaser); dormente → 404. Runtime puro.
"""
from __future__ import annotations

import app.models  # noqa: F401

from datetime import date, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.pet_profile_config import PetProfileConfig
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.models.walk import Walk
from app.routes import pet_health as routes


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


def test_achievements_full_shape(monkeypatch):
    db = _ctx()
    db.add(Walk(id="w0", tutor_id="u1", pet_id="p1", tenant_id="t1",
                scheduled_date="2026-07-01", duration_minutes=30, price=0.0,
                status="completed", created_at=datetime.utcnow() - timedelta(days=1)))
    db.commit()
    c = _client(db, db.get(User, "u1"), True, monkeypatch)

    r = c.get("/api/pets/p1/achievements")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pet_id"] == "p1"
    assert body["summary"]["total"] == 10
    assert body["summary"]["achieved"] >= 1
    assert len(body["achievements"]) == 10
    first = body["achievements"][0]
    assert set(first) == {
        "key", "category", "label", "description", "achieved",
        "achieved_at", "progress", "offer_hint",
    }


def test_achievements_admin_of_tenant_allowed(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "adm"), True, monkeypatch)
    assert c.get("/api/pets/p1/achievements").status_code == 200


def test_achievements_non_owner_404(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "u2"), True, monkeypatch)
    assert c.get("/api/pets/p1/achievements").status_code == 404


def test_achievements_gate_off_404(monkeypatch):
    db = _ctx(active=False)
    c = _client(db, db.get(User, "u1"), False, monkeypatch)
    assert c.get("/api/pets/p1/achievements").status_code == 404


def test_achievements_free_plan_403_teaser(monkeypatch):
    db = _ctx(active=True, plan="free", with_trial=False)
    c = _client(db, db.get(User, "u1"), True, monkeypatch)
    r = c.get("/api/pets/p1/achievements")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "plan_upgrade_required"
    assert r.json()["detail"]["feature"] == "pet_achievements"


def test_achievements_free_plan_trial_allows(monkeypatch):
    db = _ctx(active=True, plan="free", with_trial=True)
    c = _client(db, db.get(User, "u1"), True, monkeypatch)
    assert c.get("/api/pets/p1/achievements").status_code == 200


def test_achievements_missing_pet_404(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "u1"), True, monkeypatch)
    assert c.get("/api/pets/nope/achievements").status_code == 404
