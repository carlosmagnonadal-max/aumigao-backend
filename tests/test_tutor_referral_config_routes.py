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
from app.routes import tutor_referral_config as routes


def _client():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan="business"))
    db.commit()
    admin = User(id="a1", email="a@x.com", password_hash="x", role="super_admin", tenant_id="t1")
    app = FastAPI()
    app.include_router(routes.api_admin_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: admin
    return TestClient(app)


def test_get_creates_default_off():
    c = _client()
    r = c.get("/api/admin/tutor-referral-config")
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    assert r.json()["reward_type"] == "desconto"


def test_put_updates_and_persists():
    c = _client()
    r = c.put("/api/admin/tutor-referral-config", json={"enabled": True, "reward_type": "credito", "credit_walks": 2})
    assert r.status_code == 200
    assert r.json()["enabled"] is True
    assert r.json()["reward_type"] == "credito"
    assert c.get("/api/admin/tutor-referral-config").json()["credit_walks"] == 2


def test_put_rejects_bad_value():
    c = _client()
    r = c.put("/api/admin/tutor-referral-config", json={"reward_type": "banana"})
    assert r.status_code == 422
