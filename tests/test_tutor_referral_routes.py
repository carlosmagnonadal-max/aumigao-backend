from __future__ import annotations

import app.models  # noqa: F401

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.user import User
from app.routes import tutor_referrals as routes


def _ctx():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan="business"))
    db.add(User(id="u1", email="u1@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.commit()
    return db


def _client(db, user):
    app = FastAPI()
    app.include_router(routes.api_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def test_create_returns_code():
    db = _ctx()
    c = _client(db, db.get(User, "u1"))
    r = c.post("/api/referrals/tutors")
    assert r.status_code == 200
    assert r.json()["referral_code"].startswith("TUT-")


def test_validate_code_public():
    db = _ctx()
    c = _client(db, db.get(User, "u1"))
    code = c.post("/api/referrals/tutors").json()["referral_code"]
    r = c.post("/api/referrals/tutors/validate-code", json={"code": code})
    assert r.status_code == 200
    assert r.json()["tenant_slug"] == "t1"
