from __future__ import annotations

import app.models  # noqa: F401

from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.models.pet import Pet
from app.models.pet_profile_config import PetProfileConfig
from app.routes import pet_profile as routes


def _ctx(active=True):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan="business"))
    db.add(User(id="u1", email="u1@x.com", password_hash="x", role="tutor", tenant_id="t1"))
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


def test_dormant_returns_404(monkeypatch):
    db = _ctx(active=False)
    c = _client(db, db.get(User, "u1"), env=False, monkeypatch=monkeypatch)
    assert c.get("/api/pets/p1/timeline").status_code == 404


def test_add_and_list_event(monkeypatch):
    db = _ctx(active=True)
    c = _client(db, db.get(User, "u1"), env=True, monkeypatch=monkeypatch)
    r = c.post("/api/pets/p1/timeline", json={"event_type": "weight", "title": "Peso 10kg",
                                              "occurred_at": "2026-07-01T00:00:00"})
    assert r.status_code == 201
    lst = c.get("/api/pets/p1/timeline")
    assert lst.status_code == 200
    assert len(lst.json()["events"]) == 1


def test_reject_future_occurred_at(monkeypatch):
    db = _ctx(active=True)
    c = _client(db, db.get(User, "u1"), env=True, monkeypatch=monkeypatch)
    r = c.post("/api/pets/p1/timeline", json={"event_type": "weight", "title": "x",
                                              "occurred_at": "2999-01-01T00:00:00"})
    assert r.status_code == 422


def test_ownership_other_users_pet_404(monkeypatch):
    db = _ctx(active=True)
    db.add(User(id="u2", email="u2@x.com", password_hash="x", role="tutor", tenant_id="t1")); db.commit()
    c = _client(db, db.get(User, "u2"), env=True, monkeypatch=monkeypatch)
    assert c.get("/api/pets/p1/timeline").status_code == 404
